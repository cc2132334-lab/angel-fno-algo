import os
import time
import json
import pyotp
import requests
import datetime
import threading
import pandas as pd
from dateutil import tz
from flask import Flask, render_template, request, jsonify
from SmartApi import SmartConnect

app = Flask(__name__)

IST = tz.gettz('Asia/Kolkata')
CONFIG_FILE = "bot_config.json"

def get_ist_now():
    return datetime.datetime.now(IST)

def load_saved_config():
    default_config = {
        "engine_running": True,
        "max_trades": 3,
        "rr_ratio": 3,
        "cutoff_time": "15:00",
        "risk_amount": 500,
        "trading_mode": "PAPER"
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return {**default_config, **json.load(f)}
        except Exception:
            pass
    return default_config

def save_config():
    try:
        data = {
            "engine_running": bot_state["engine_running"],
            "max_trades": bot_state["max_trades"],
            "rr_ratio": bot_state["rr_ratio"],
            "cutoff_time": bot_state["cutoff_time"],
            "risk_amount": bot_state["risk_amount"],
            "trading_mode": bot_state["trading_mode"]
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Config save error: {e}")

saved_conf = load_saved_config()

bot_state = {
    "is_logged_in": False,
    "engine_running": saved_conf["engine_running"],
    "smart_api": None,
    "feed_token": None,
    "risk_amount": saved_conf["risk_amount"],
    "trading_mode": saved_conf["trading_mode"],
    "max_trades": saved_conf["max_trades"],
    "rr_ratio": saved_conf["rr_ratio"],
    "cutoff_time": saved_conf["cutoff_time"],
    "trades_executed_today": 0,
    "fno_stocks": [],              # List with symbol, token, pdh, pdl
    "total_pnl": 0.0,
    "market_indices": {
        "NIFTY": {"ltp": 0.0, "change": 0.0, "pchange": 0.0},
        "SENSEX": {"ltp": 0.0, "change": 0.0, "pchange": 0.0}
    },
    "market_movers": {
        "gainers": [],
        "losers": []
    },
    "c1_candidates": [],
    "active_trades": [],
    "trade_history": [],
    "status_log": "Terminal Ready. Waiting for Angel One Login."
}

def log(msg):
    timestamp = get_ist_now().strftime("%H:%M:%S")
    bot_state["status_log"] = f"[{timestamp} IST] {msg}"
    print(bot_state["status_log"])

def calculate_quantity(risk_amount, entry_price, sl_price):
    """Quantity = Risk Per Trade / SL Points"""
    try:
        sl_points = abs(entry_price - sl_price)
        if sl_points <= 0.05:
            return 1
        qty = int(risk_amount / sl_points)
        return max(1, qty)
    except Exception:
        return 1

def fetch_candles(token, interval="FIVE_MINUTE", days=2):
    now = get_ist_now()
    from_date = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d 09:15")
    to_date = now.strftime("%Y-%m-%d %H:%M")
    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date
    }
    try:
        data = bot_state["smart_api"].getCandleData(params)
        if data and data.get("status") and data.get("data"):
            return pd.DataFrame(data["data"], columns=["time", "open", "high", "low", "close", "volume"])
    except Exception:
        pass
    return None

def fetch_daily_pdh_pdl(token):
    """Pichle trading day ka High aur Low (PDH/PDL) fetch karta hai"""
    now = get_ist_now()
    from_date = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d 09:15")
    to_date = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d 15:30")
    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": "ONE_DAY",
        "fromdate": from_date,
        "todate": to_date
    }
    try:
        data = bot_state["smart_api"].getCandleData(params)
        if data and data.get("status") and data.get("data"):
            df = pd.DataFrame(data["data"], columns=["time", "open", "high", "low", "close", "volume"])
            if not df.empty:
                last_day = df.iloc[-1]
                return float(last_day["high"]), float(last_day["low"]), float(last_day["close"])
    except Exception:
        pass
    return None, None, None

def load_fno_universe():
    """Angel One Scrip Master se automatically sare F&O Cash stocks detect karta hai"""
    try:
        log("Auto-detecting all active F&O Cash stocks from Angel Master...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = requests.get(url, timeout=20)
        master = res.json()

        # Step 1: Active F&O Futures ke symbols dhoondna
        fno_names = set()
        for s in master:
            if s.get('exch_seg') == 'NFO' and s.get('instrumenttype') == 'FUTSTK':
                name = s.get('name')
                if name:
                    fno_names.add(name.strip())

        # Step 2: Unhi symbols ke NSE Cash (-EQ) tokens map karna
        matched_stocks = []
        for s in master:
            if s.get('exch_seg') == 'NSE' and str(s.get('symbol', '')).endswith('-EQ'):
                sym_clean = s.get('name', '').strip()
                if sym_clean in fno_names:
                    matched_stocks.append({
                        "symbol": s.get('symbol'),
                        "name": sym_clean,
                        "token": str(s.get('token'))
                    })

        log(f"Found {len(matched_stocks)} active F&O Cash stocks. Fetching PDH & PDL...")

        # Step 3: Har stock ke liye PDH aur PDL nikalna
        final_list = []
        for item in matched_stocks:
            time.sleep(0.08)  # API rate limit safety
            pdh, pdl, prev_close = fetch_daily_pdh_pdl(item["token"])
            if pdh and pdl:
                final_list.append({
                    "symbol": item["symbol"],
                    "token": item["token"],
                    "pdh": pdh,
                    "pdl": pdl,
                    "prev_close": prev_close or pdl
                })

        bot_state["fno_stocks"] = final_list
        log(f"Ready! Loaded {len(bot_state['fno_stocks'])} FnO stocks with PDH & PDL.")
    except Exception as e:
        log(f"Error loading universe: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    try:
        smart_api = SmartConnect(api_key=data.get("api_key"))
        totp = pyotp.TOTP(data.get("totp_secret")).now()
        login_res = smart_api.generateSession(data.get("client_code"), data.get("pin"), totp)

        if login_res.get('status'):
            bot_state["smart_api"] = smart_api
            bot_state["feed_token"] = smart_api.getfeedToken()
            bot_state["is_logged_in"] = True
            log("Broker Connected! Engine initialized.")

            threading.Thread(target=load_fno_universe, daemon=True).start()
            threading.Thread(target=background_scanner, daemon=True).start()
            threading.Thread(target=market_data_monitor, daemon=True).start()

            return jsonify({"status": "success", "message": "Login Successful!"})
        else:
            return jsonify({"status": "error", "message": login_res.get("message", "Login failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/update-settings', methods=['POST'])
def update_settings():
    data = request.json or {}
    if "max_trades" in data:
        bot_state["max_trades"] = int(data["max_trades"])
    if "rr_ratio" in data:
        bot_state["rr_ratio"] = int(data["rr_ratio"])
    if "cutoff_time" in data:
        bot_state["cutoff_time"] = str(data["cutoff_time"])
    if "risk_amount" in data:
        bot_state["risk_amount"] = int(data["risk_amount"])
    save_config()
    log("Settings updated.")
    return jsonify({"status": "success"})

@app.route('/api/toggle-engine', methods=['POST'])
def toggle_engine():
    data = request.json or {}
    bot_state["engine_running"] = data.get("running", True)
    save_config()
    status_str = "RUNNING" if bot_state["engine_running"] else "STOPPED"
    log(f"Engine status: {status_str}")
    return jsonify({"status": "success", "engine_running": bot_state["engine_running"]})

@app.route('/api/set-mode', methods=['POST'])
def set_mode():
    data = request.json or {}
    mode = data.get("mode", "PAPER").upper()
    if mode in ["PAPER", "LIVE"]:
        bot_state["trading_mode"] = mode
        save_config()
        log(f"Mode changed to: {mode}")
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error"})

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify({
        "logged_in": bot_state["is_logged_in"],
        "engine_running": bot_state["engine_running"],
        "trading_mode": bot_state["trading_mode"],
        "max_trades": bot_state["max_trades"],
        "rr_ratio": bot_state["rr_ratio"],
        "cutoff_time": bot_state["cutoff_time"],
        "risk_amount": bot_state["risk_amount"],
        "trades_executed_today": bot_state["trades_executed_today"],
        "status": bot_state["status_log"],
        "total_pnl": bot_state["total_pnl"],
        "market_indices": bot_state["market_indices"],
        "market_movers": bot_state["market_movers"],
        "c1_candidates": bot_state["c1_candidates"],
        "active_trades": bot_state["active_trades"],
        "trade_history": bot_state["trade_history"]
    })

def place_live_order(symbol, token, side, qty):
    try:
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": str(token),
            "transactiontype": side,
            "exchange": "NSE",
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(qty)
        }
        res = bot_state["smart_api"].placeOrder(order_params)
        log(f"LIVE ORDER SENT: {side} {qty} {symbol} | Response: {res}")
        return res
    except Exception as e:
        log(f"LIVE ORDER FAILED: {e}")
        return None

def background_scanner():
    c1_scanned = False
    c2_scanned = False

    while bot_state["is_logged_in"]:
        if not bot_state["engine_running"]:
            time.sleep(1)
            continue

        now_ist = get_ist_now()
        now_time = now_ist.time()

        cutoff_parts = [int(x) for x in bot_state["cutoff_time"].split(":")]
        if now_time >= datetime.time(cutoff_parts[0], cutoff_parts[1]):
            time.sleep(5)
            continue

        if bot_state["trades_executed_today"] >= bot_state["max_trades"]:
            time.sleep(5)
            continue

        # ---------------------------------------------------------
        # 1. 09:20:02 AM - SCAN C1 FOR 5x VOLUME + PDH/PDL BREAKOUT
        # ---------------------------------------------------------
        if not c1_scanned and now_time >= datetime.time(9, 20, 2):
            log("09:20 AM: Scanning C1 for 5x Volume AND PDH/PDL Breakout...")
            candidates = []

            for item in bot_state["fno_stocks"]:
                time.sleep(0.12)
                df = fetch_candles(item["token"])
                if df is not None and len(df) >= 22:
                    avg_vol = df.iloc[-22:-2]["volume"].mean()
                    c1_candle = df.iloc[-2]
                    c1_vol = float(c1_candle["volume"])
                    c1_close = float(c1_candle["close"])
                    c1_high = float(c1_candle["high"])
                    c1_low = float(c1_candle["low"])
                    pdh = item["pdh"]
                    pdl = item["pdl"]

                    # Filter: Volume >= 5x
                    if avg_vol > 0 and (c1_vol >= 5 * avg_vol):
                        bias = None

                        # Filter: Close > PDH ya Close < PDL
                        if c1_close > pdh:
                            bias = "BULLISH_PDH_BREAKOUT"
                        elif c1_close < pdl:
                            bias = "BEARISH_PDL_BREAKDOWN"

                        if bias:
                            candidates.append({
                                "symbol": item["symbol"],
                                "token": item["token"],
                                "bias": bias,
                                "c1_high": c1_high,
                                "c1_low": c1_low,
                                "c1_close": c1_close,
                                "c1_vol": int(c1_vol),
                                "pdh": pdh,
                                "pdl": pdl,
                                "ratio": round(c1_vol / avg_vol, 2)
                            })
                            log(f"Qualified: {item['symbol']} ({round(c1_vol/avg_vol, 2)}x Vol) [{bias}]")

            bot_state["c1_candidates"] = candidates
            log(f"C1 Scan Complete: {len(candidates)} candidate(s) ready for C2 validation.")
            c1_scanned = True

        # ---------------------------------------------------------
        # 2. 09:25:02 AM - C2 SETUP VALIDATION & ORDER EXECUTION
        # ---------------------------------------------------------
        if c1_scanned and not c2_scanned and now_time >= datetime.time(9, 25, 2):
            log("09:25 AM: Validating C2 Setup and Triggering Orders...")

            for cand in bot_state["c1_candidates"]:
                if bot_state["trades_executed_today"] >= bot_state["max_trades"]:
                    log("Daily max trades limit reached.")
                    break

                time.sleep(0.2)
                df = fetch_candles(cand["token"])
                if df is not None and len(df) >= 2:
                    c2 = df.iloc[-1]
                    c2_high = float(c2["high"])
                    c2_low = float(c2["low"])
                    c2_close = float(c2["close"])

                    c1_h = cand["c1_high"]
                    c1_l = cand["c1_low"]

                    side = None
                    trigger_price = 0.0
                    sl = 0.0

                    # BUY SETUP (Bullish PDH Breakout)
                    if cand["bias"] == "BULLISH_PDH_BREAKOUT":
                        if c2_high > c1_h:
                            if c2_close > c1_h:
                                side = "BUY"
                                trigger_price = c2_high
                                sl = c2_low
                            else:
                                log(f"{cand['symbol']} Invalid: C2 High crossed C1 High but failed to close above.")
                        elif c2_high <= c1_h and c2_low >= c1_l:
                            # Inside Bar
                            side = "BUY"
                            trigger_price = c1_h
                            sl = c2_low

                    # SELL SETUP (Bearish PDL Breakdown)
                    elif cand["bias"] == "BEARISH_PDL_BREAKDOWN":
                        if c2_low < c1_l:
                            if c2_close < c1_l:
                                side = "SELL"
                                trigger_price = c2_low
                                sl = c2_high
                            else:
                                log(f"{cand['symbol']} Invalid: C2 Low crossed C1 Low but failed to close below.")
                        elif c2_high <= c1_h and c2_low >= c1_l:
                            # Inside Bar
                            side = "SELL"
                            trigger_price = c1_l
                            sl = c2_high

                    # Order Placement
                    if side:
                        risk_pts = abs(trigger_price - sl)
                        qty = calculate_quantity(bot_state["risk_amount"], trigger_price, sl)
                        target_mult = bot_state["rr_ratio"]
                        final_target = round(trigger_price + (target_mult * risk_pts) if side == "BUY" else trigger_price - (target_mult * risk_pts), 2)

                        mode = bot_state["trading_mode"]
                        status = "OPEN"
                        if mode == "LIVE":
                            res = place_live_order(cand["symbol"], cand["token"], side, qty)
                            if not res:
                                status = "FAILED"

                        bot_state["active_trades"].append({
                            "id": len(bot_state["active_trades"]) + 1,
                            "symbol": cand["symbol"],
                            "token": cand["token"],
                            "side": side,
                            "trigger_price": trigger_price,
                            "entry": trigger_price,
                            "sl": sl,
                            "orig_sl": sl,
                            "target": final_target,
                            "rr_ratio": target_mult,
                            "qty": qty,
                            "remaining_qty": qty,
                            "half_booked": False,
                            "ltp": trigger_price,
                            "pnl": 0.0,
                            "status": status,
                            "mode": mode,
                            "time": get_ist_now().strftime("%H:%M:%S")
                        })
                        bot_state["trades_executed_today"] += 1
                        log(f"Trade Entered [{mode}]: {side} {cand['symbol']} Qty:{qty} Entry:{trigger_price} SL:{sl} Target:{final_target}")

            c2_scanned = True

        time.sleep(1)

def market_data_monitor():
    """Live Nifty/Sensex, Market Movers aur Active Trades ko monitor karta hai"""
    last_movers_check = 0

    while bot_state["is_logged_in"]:
        now_ts = time.time()
        try:
            # 1. Nifty Spot
            n_res = bot_state["smart_api"].ltpData("NSE", "NIFTY", "99926000")
            if n_res and n_res.get("data"):
                ltp = float(n_res["data"]["ltp"])
                close = float(n_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["NIFTY"] = {"ltp": ltp, "change": change, "pchange": pchange}

            # 2. Sensex Spot
            s_res = bot_state["smart_api"].ltpData("BSE", "SENSEX", "99919000")
            if s_res and s_res.get("data"):
                ltp = float(s_res["data"]["ltp"])
                close = float(s_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["SENSEX"] = {"ltp": ltp, "change": change, "pchange": pchange}
        except Exception:
            pass

        # 3. Market Movers Update (Har 15 seconds me calculate hoga)
        if now_ts - last_movers_check > 15 and bot_state["fno_stocks"]:
            last_movers_check = now_ts
            movers_data = []
            sample_stocks = bot_state["fno_stocks"][:30]  # Fast calculation for top liquid stocks
            for st in sample_stocks:
                try:
                    res = bot_state["smart_api"].ltpData("NSE", st["symbol"], str(st["token"]))
                    if res and res.get("data"):
                        ltp = float(res["data"]["ltp"])
                        close = float(st.get("prev_close") or ltp)
                        p_chg = round(((ltp - close) / close) * 100, 2) if close > 0 else 0.0
                        movers_data.append({
                            "symbol": st["symbol"].replace("-EQ", ""),
                            "ltp": ltp,
                            "pchange": p_chg
                        })
                except Exception:
                    pass
            if movers_data:
                sorted_movers = sorted(movers_data, key=lambda x: x["pchange"], reverse=True)
                bot_state["market_movers"]["gainers"] = sorted_movers[:5]
                bot_state["market_movers"]["losers"] = sorted(movers_data, key=lambda x: x["pchange"])[:5]

        # 4. Active Trades Management (1:2 Half Booking & Trailing)
        open_trades = [t for t in bot_state["active_trades"] if t["status"] == "OPEN"]
        for trade in open_trades:
            try:
                ltp_data = bot_state["smart_api"].ltpData("NSE", trade["symbol"], str(trade["token"]))
                if ltp_data and ltp_data.get("data"):
                    ltp = float(ltp_data["data"]["ltp"])
                    trade["ltp"] = ltp
                    risk_unit = abs(trade["entry"] - trade["orig_sl"])

                    if trade["side"] == "BUY":
                        trade["pnl"] = round((ltp - trade["entry"]) * trade["remaining_qty"], 2)

                        # 1:2 Milestone: 50% book aur SL to Cost
                        if not trade["half_booked"] and ltp >= (trade["entry"] + 2 * risk_unit):
                            half_qty = max(1, trade["remaining_qty"] // 2)
                            trade["remaining_qty"] -= half_qty
                            trade["half_booked"] = True
                            trade["sl"] = trade["entry"]
                            log(f"1:2 Hit on {trade['symbol']}! Booked 50%. SL trailed to Cost.")

                        if ltp <= trade["sl"]:
                            trade["status"] = "SL / TRAIL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "SELL", trade["remaining_qty"])
                            record_trade_history(trade, ltp)
                        elif ltp >= trade["target"]:
                            trade["status"] = "FULL TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "SELL", trade["remaining_qty"])
                            record_trade_history(trade, ltp)

                    else:  # SELL Side
                        trade["pnl"] = round((trade["entry"] - ltp) * trade["remaining_qty"], 2)

                        # 1:2 Milestone
                        if not trade["half_booked"] and ltp <= (trade["entry"] - 2 * risk_unit):
                            half_qty = max(1, trade["remaining_qty"] // 2)
                            trade["remaining_qty"] -= half_qty
                            trade["half_booked"] = True
                            trade["sl"] = trade["entry"]
                            log(f"1:2 Hit on {trade['symbol']}! Booked 50%. SL trailed to Cost.")

                        if ltp <= trade["sl"]:
                            trade["status"] = "SL / TRAIL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                            record_trade_history(trade, ltp)
                        elif ltp >= trade["target"]:
                            trade["status"] = "FULL TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                            record_trade_history(trade, ltp)
            except Exception:
                pass
            time.sleep(0.1)

        bot_state["total_pnl"] = round(sum(t["pnl"] for t in bot_state["active_trades"]), 2)
        time.sleep(2)

def record_trade_history(trade, exit_price):
    bot_state["trade_history"].append({
        "time": get_ist_now().strftime("%H:%M:%S"),
        "symbol": trade["symbol"],
        "side": trade["side"],
        "entry": trade["entry"],
        "exit": exit_price,
        "pnl": trade["pnl"],
        "status": trade["status"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
