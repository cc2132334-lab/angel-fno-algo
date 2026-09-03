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

# Indian Standard Time (IST)
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
        config_to_save = {
            "engine_running": bot_state["engine_running"],
            "max_trades": bot_state["max_trades"],
            "rr_ratio": bot_state["rr_ratio"],
            "cutoff_time": bot_state["cutoff_time"],
            "risk_amount": bot_state["risk_amount"],
            "trading_mode": bot_state["trading_mode"]
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(config_to_save, f)
    except Exception as e:
        print(f"Error saving config: {e}")

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
    "fno_stocks": [],
    "total_pnl": 0.0,
    "market_indices": {
        "NIFTY": {"ltp": 0.0, "change": 0.0, "pchange": 0.0},
        "SENSEX": {"ltp": 0.0, "change": 0.0, "pchange": 0.0}
    },
    "market_stats": {
        "top_gainers": [],
        "top_losers": [],
        "volume_buzzers": []
    },
    "c1_candidates": [],
    "active_trades": [],
    "trade_history": [],
    "status_log": "Broker disconnected. Please login to continue."
}

def log(msg):
    timestamp = get_ist_now().strftime("%H:%M:%S")
    bot_state["status_log"] = f"[{timestamp} IST] {msg}"
    print(bot_state["status_log"])

def calculate_quantity(risk_amount, entry_price, sl_price):
    """
    FORMULA: Quantity = Risk Per Trade / SL Points
    """
    try:
        sl_points = abs(entry_price - sl_price)
        if sl_points <= 0.05:
            return 1
        qty = int(risk_amount / sl_points)
        return max(1, qty)
    except Exception as e:
        print(f"Error calculating quantity: {e}")
        return 1

def load_fno_symbols():
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = requests.get(url, timeout=12)
        master = res.json()

        nse_equities = {
            s['symbol'].replace('-EQ', ''): s['token'] 
            for s in master 
            if s.get('exch_seg') == 'NSE' and str(s.get('symbol')).endswith('-EQ')
        }

        fno_csv_url = "https://archives.nseindia.com/content/fo/fo_mktlots.csv"
        headers = {'User-Agent': 'Mozilla/5.0'}
        fno_res = requests.get(fno_csv_url, headers=headers, timeout=12)

        stocks = []
        for line in fno_res.text.split('\n')[5:]:
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 2:
                sym = parts[1]
                if sym in nse_equities:
                    stocks.append({"symbol": f"{sym}-EQ", "token": nse_equities[sym]})

        bot_state["fno_stocks"] = stocks[:150]
        log(f"Loaded {len(bot_state['fno_stocks'])} FnO Cash Stocks.")
    except Exception as e:
        log(f"Error loading scrip master: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    try:
        smart_api = SmartConnect(api_key=data.get("api_key"))
        totp = pyotp.TOTP(data.get("totp_secret")).now()
        login_res = smart_api.generateSession(data.get("client_code"), data.get("pin"), totp)

        if login_res.get('status'):
            bot_state["smart_api"] = smart_api
            bot_state["feed_token"] = smart_api.getfeedToken()
            bot_state["is_logged_in"] = True
            log("Broker Connected Successfully! Terminal Unlocked.")

            load_fno_symbols()
            threading.Thread(target=background_scanner, daemon=True).start()
            threading.Thread(target=market_data_monitor, daemon=True).start()
            threading.Thread(target=market_stats_scanner, daemon=True).start()

            return jsonify({"status": "success", "message": "Login Successful!"})
        else:
            return jsonify({"status": "error", "message": login_res.get("message", "Login failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/update-settings', methods=['POST'])
def update_settings():
    data = request.json
    if "max_trades" in data:
        bot_state["max_trades"] = int(data["max_trades"])
    if "rr_ratio" in data:
        bot_state["rr_ratio"] = int(data["rr_ratio"])
    if "cutoff_time" in data:
        bot_state["cutoff_time"] = str(data["cutoff_time"])
    if "risk_amount" in data:
        bot_state["risk_amount"] = int(data["risk_amount"])
    save_config()
    log(f"Settings saved: MaxTrades={bot_state['max_trades']}, Target=1:{bot_state['rr_ratio']}, Cutoff={bot_state['cutoff_time']}, Risk=₹{bot_state['risk_amount']}")
    return jsonify({"status": "success", "message": "Settings updated"})

@app.route('/api/toggle-engine', methods=['POST'])
def toggle_engine():
    data = request.json
    bot_state["engine_running"] = data.get("running", True)
    save_config()
    status_str = "RUNNING" if bot_state["engine_running"] else "STOPPED"
    log(f"Engine status: {status_str}")
    return jsonify({"status": "success", "engine_running": bot_state["engine_running"]})

@app.route('/api/set-mode', methods=['POST'])
def set_mode():
    data = request.json
    mode = data.get("mode", "PAPER").upper()
    if mode in ["PAPER", "LIVE"]:
        bot_state["trading_mode"] = mode
        save_config()
        log(f"Trading Mode: {mode}")
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error", "message": "Invalid mode"})

@app.route('/api/manual-5x-scan', methods=['POST'])
def manual_5x_scan():
    """Selected Date ke hisab se FnO Cash pool me 5x Volume scan karna (User Analysis ke liye)"""
    if not bot_state["is_logged_in"]:
        return jsonify({"status": "error", "message": "Please connect broker first"})

    data = request.json
    selected_date = data.get("date")
    if not selected_date:
        return jsonify({"status": "error", "message": "Please select a valid date"})

    try:
        target_dt = datetime.datetime.strptime(selected_date, "%Y-%m-%d")
        from_dt = (target_dt - datetime.timedelta(days=5)).strftime("%Y-%m-%d 09:15")
        to_dt = target_dt.strftime("%Y-%m-%d 15:30")
    except Exception:
        return jsonify({"status": "error", "message": "Invalid date format"})

    filtered_results = []
    stocks_to_scan = bot_state["fno_stocks"] if bot_state["fno_stocks"] else []
    if not stocks_to_scan:
        return jsonify({"status": "error", "message": "No stocks available in F&O cache yet"})

    for item in stocks_to_scan[:60]:
        time.sleep(0.25)
        params = {
            "exchange": "NSE",
            "symboltoken": str(item["token"]),
            "interval": "FIVE_MINUTE",
            "fromdate": from_dt,
            "todate": to_dt
        }
        try:
            res = bot_state["smart_api"].getCandleData(params)
            if res and res.get("status") and res.get("data"):
                df = pd.DataFrame(res["data"], columns=["time", "open", "high", "low", "close", "volume"])
                df['date_str'] = df['time'].apply(lambda x: str(x).split('T')[0] if 'T' in str(x) else str(x).split(' ')[0])
                
                day_df = df[df['date_str'] == selected_date]
                if not day_df.empty and len(day_df) >= 1:
                    c1_idx = day_df.index[0]
                    if c1_idx >= 20:
                        prev_20 = df.iloc[c1_idx-20:c1_idx]
                        avg_vol = prev_20["volume"].mean()
                        c1_candle = df.iloc[c1_idx]
                        c1_vol = float(c1_candle["volume"])

                        if avg_vol > 0 and (c1_vol >= 5 * avg_vol):
                            filtered_results.append({
                                "symbol": item["symbol"].replace("-EQ", ""),
                                "c1_high": float(c1_candle["high"]),
                                "c1_low": float(c1_candle["low"]),
                                "c1_volume": int(c1_vol),
                                "avg_volume": int(avg_vol),
                                "multiplier": round(c1_vol / avg_vol, 2)
                            })
        except Exception:
            continue

    return jsonify({"status": "success", "date": selected_date, "count": len(filtered_results), "results": filtered_results})

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
        "market_stats": bot_state["market_stats"],
        "active_trades": bot_state["active_trades"],
        "trade_history": bot_state["trade_history"]
    })

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
        log(f"LIVE ORDER ERROR: {e}")
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
        cutoff_dt_time = datetime.time(cutoff_parts[0], cutoff_parts[1])

        if now_time >= cutoff_dt_time:
            time.sleep(5)
            continue

        if bot_state["trades_executed_today"] >= bot_state["max_trades"]:
            time.sleep(5)
            continue

        # 09:20 AM IST - Scan C1 Volume (>= 5x avg)
        if not c1_scanned and now_time >= datetime.time(9, 20, 2):
            log("Scanning 9:15-9:20 AM C1 Volume (>= 5x avg)...")
            candidates = []

            for item in bot_state["fno_stocks"]:
                time.sleep(0.3)
                df = fetch_candles(item["token"])
                if df is not None and len(df) >= 22:
                    avg_vol = df.iloc[-22:-2]["volume"].mean()
                    c1_candle = df.iloc[-2]
                    c1_vol = c1_candle["volume"]

                    if avg_vol > 0 and (c1_vol >= 5 * avg_vol):
                        candidates.append({
                            "symbol": item["symbol"],
                            "token": item["token"],
                            "c1_high": float(c1_candle["high"]),
                            "c1_low": float(c1_candle["low"]),
                            "c1_vol": int(c1_vol)
                        })

            bot_state["c1_candidates"] = candidates
            log(f"C1 Volume Scan complete: {len(candidates)} candidate stocks found.")
            c1_scanned = True

        # 09:25 AM IST - Check C2 Rules
        if c1_scanned and not c2_scanned and now_time >= datetime.time(9, 25, 2):
            log("Evaluating C2 Setup Rules...")

            for cand in bot_state["c1_candidates"]:
                if bot_state["trades_executed_today"] >= bot_state["max_trades"]:
                    log("Daily max trades limit reached.")
                    break

                time.sleep(0.3)
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

                    # BUY RULE
                    if c2_high > c1_h:
                        if c2_close > c1_h:
                            side = "BUY"
                            trigger_price = c2_high
                            sl = c2_low
                        else:
                            log(f"{cand['symbol']} BUY Invalid: C2 High breached C1 High but close wasn't above it.")
                    elif c2_high <= c1_h and c2_low >= c1_l:
                        # Inside bar
                        side = "BUY"
                        trigger_price = c1_h
                        sl = c2_low

                    # SELL RULE
                    if not side:
                        if c2_low < c1_l:
                            if c2_close < c1_l:
                                side = "SELL"
                                trigger_price = c2_low
                                sl = c2_high
                            else:
                                log(f"{cand['symbol']} SELL Invalid: C2 Low breached C1 Low but close wasn't below it.")
                        elif c2_high <= c1_h and c2_low >= c1_l:
                            # Inside bar
                            side = "SELL"
                            trigger_price = c1_l
                            sl = c2_high

                    if side:
                        risk_pts = abs(trigger_price - sl)
                        # Exact requested formula: Risk Per Trade / SL Points
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
                        log(f"Trade Entered [{mode}]: {side} {cand['symbol']} Qty:{qty} SL:{sl} Target:{final_target}")

            c2_scanned = True

        time.sleep(1)

def market_data_monitor():
    while bot_state["is_logged_in"]:
        try:
            n_res = bot_state["smart_api"].ltpData("NSE", "NIFTY", "99926000")
            if n_res and n_res.get("data"):
                ltp = float(n_res["data"]["ltp"])
                close = float(n_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["NIFTY"] = {"ltp": ltp, "change": change, "pchange": pchange}

            s_res = bot_state["smart_api"].ltpData("BSE", "SENSEX", "99919000")
            if s_res and s_res.get("data"):
                ltp = float(s_res["data"]["ltp"])
                close = float(s_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["SENSEX"] = {"ltp": ltp, "change": change, "pchange": pchange}
        except Exception:
            pass

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
                        # 1:2 Milestone: 50% Qty Book & Trail to Entry
                        if not trade["half_booked"] and ltp >= (trade["entry"] + 2 * risk_unit):
                            half_qty = max(1, trade["remaining_qty"] // 2)
                            trade["remaining_qty"] -= half_qty
                            trade["half_booked"] = True
                            trade["sl"] = trade["entry"]
                            log(f"1:2 Milestone Hit for {trade['symbol']}! Booked 50% qty. SL trailed to entry (Cost).")

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

                    else:
                        trade["pnl"] = round((trade["entry"] - ltp) * trade["remaining_qty"], 2)
                        if not trade["half_booked"] and ltp <= (trade["entry"] - 2 * risk_unit):
                            half_qty = max(1, trade["remaining_qty"] // 2)
                            trade["remaining_qty"] -= half_qty
                            trade["half_booked"] = True
                            trade["sl"] = trade["entry"]
                            log(f"1:2 Milestone Hit for {trade['symbol']}! Booked 50% qty. SL trailed to entry (Cost).")

                        if ltp >= trade["sl"]:
                            trade["status"] = "SL / TRAIL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                            record_trade_history(trade, ltp)
                        elif ltp <= trade["target"]:
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

def market_stats_scanner():
    while bot_state["is_logged_in"]:
        stock_perf = []
        scan_pool = bot_state["fno_stocks"][:40]

        for s in scan_pool:
            try:
                ltp_data = bot_state["smart_api"].ltpData("NSE", s["symbol"], str(s["token"]))
                if ltp_data and ltp_data.get("data"):
                    d = ltp_data["data"]
                    ltp = float(d["ltp"])
                    close = float(d.get("close", ltp))
                    pchange = round(((ltp - close) / close) * 100, 2) if close > 0 else 0.0
                    vol = int(d.get("trade_volume", 0) or 0)
                    stock_perf.append({"symbol": s["symbol"].replace("-EQ", ""), "ltp": ltp, "pchange": pchange, "volume": vol})
            except Exception:
                pass
            time.sleep(0.2)

        if stock_perf:
            df_perf = pd.DataFrame(stock_perf)
            gainers = df_perf.sort_values(by="pchange", ascending=False).head(5).to_dict('records')
            losers = df_perf.sort_values(by="pchange", ascending=True).head(5).to_dict('records')
            volume_top = df_perf.sort_values(by="volume", ascending=False).head(5).to_dict('records')

            bot_state["market_stats"]["top_gainers"] = gainers
            bot_state["market_stats"]["top_losers"] = losers
            bot_state["market_stats"]["volume_buzzers"] = volume_top

        time.sleep(25)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
