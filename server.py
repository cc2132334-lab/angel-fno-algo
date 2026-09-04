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
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Config save error: {e}")

saved_conf = load_saved_config()

bot_state = {
    "is_logged_in": False,
    "engine_running": saved_conf["engine_running"],
    "smart_api": None,
    "feed_token": None,
    "risk_amount": int(saved_conf["risk_amount"]),
    "trading_mode": saved_conf["trading_mode"],
    "max_trades": int(saved_conf["max_trades"]),
    "rr_ratio": int(saved_conf["rr_ratio"]),
    "cutoff_time": str(saved_conf["cutoff_time"]),
    "trades_executed_today": 0,
    "fno_stocks": [],
    "is_market_live": False,
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
    "scan_cancelled": False,
    "c1_candidates": [],
    "pending_orders": [],
    "active_trades": [],
    "trade_history": [],
    "system_logs": ["Terminal initialized. Waiting for broker connection..."],
    "status_log": "Terminal Ready. Please connect broker."
}

def log(msg):
    timestamp = get_ist_now().strftime("%I:%M:%S %p")
    entry = f"[{timestamp} IST] {msg}"
    bot_state["status_log"] = entry
    bot_state["system_logs"].append(entry)
    if len(bot_state["system_logs"]) > 200:
        bot_state["system_logs"].pop(0)
    print(entry)

def calculate_quantity(risk_amount, entry_price, sl_price):
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
    now = get_ist_now()
    from_date = (now - datetime.timedelta(days=12)).strftime("%Y-%m-%d 09:15")
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
                avg_5d_vol = float(df["volume"].tail(5).mean()) if len(df) >= 2 else float(last_day["volume"])
                return float(last_day["high"]), float(last_day["low"]), float(last_day["close"]), float(last_day["volume"]), avg_5d_vol
    except Exception:
        pass
    return None, None, None, 0, 1.0

def load_fno_universe():
    try:
        log("Downloading Angel One Master & strictly filtering F&O Cash stocks...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = requests.get(url, timeout=25)
        master = res.json()

        fno_symbols = set()
        for s in master:
            if s.get('exch_seg') == 'NFO' and s.get('instrumenttype') == 'FUTSTK':
                base_name = s.get('name', '').strip()
                if base_name and "TEST" not in base_name.upper():
                    fno_symbols.add(base_name.upper())

        matched_stocks = []
        for s in master:
            if s.get('exch_seg') == 'NSE':
                sym = str(s.get('symbol', ''))
                if sym.endswith('-EQ'):
                    clean_name = sym.replace('-EQ', '').strip().upper()
                    if "TEST" in clean_name or "NSETEST" in clean_name:
                        continue
                    token = str(s.get('token'))
                    if clean_name in fno_symbols:
                        matched_stocks.append({
                            "symbol": sym,
                            "name": clean_name,
                            "token": token
                        })

        log(f"Verified {len(matched_stocks)} F&O Cash stocks. Loading PDH, PDL & Volume baselines...")

        final_list = []
        for item in matched_stocks:
            time.sleep(0.015)
            pdh, pdl, prev_close, last_vol, avg_vol = fetch_daily_pdh_pdl(item["token"])
            final_list.append({
                "symbol": item["symbol"],
                "token": item["token"],
                "name": item["name"],
                "pdh": pdh or 0.0,
                "pdl": pdl or 0.0,
                "prev_close": prev_close or 0.0,
                "last_daily_vol": last_vol or 0,
                "avg_5d_vol": avg_vol if avg_vol > 0 else 1.0
            })

        bot_state["fno_stocks"] = final_list
        log(f"SUCCESS: {len(bot_state['fno_stocks'])} pure F&O Cash stocks loaded. Ready for scan.")
        update_fno_market_movers_and_buzzers()
    except Exception as e:
        log(f"Universe sync error: {e}")

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
            log("Broker Connected! Engine armed with continuous SL-M & invalidation logic.")

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
        log(f"Mode switched to: {mode}")
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error"})

@app.route('/api/manual-5x-scan', methods=['POST'])
def manual_5x_scan():
    if not bot_state.get("smart_api"):
        return jsonify({"status": "error", "message": "Please connect broker first"})

    data = request.get_json(force=True) or {}
    selected_date = data.get("date")
    if not selected_date:
        return jsonify({"status": "error", "message": "Please select a date"})

    bot_state["scan_cancelled"] = False

    try:
        target_dt = datetime.datetime.strptime(selected_date, "%Y-%m-%d")
        from_dt = (target_dt - datetime.timedelta(days=7)).strftime("%Y-%m-%d 09:15")
        to_dt = target_dt.strftime("%Y-%m-%d 15:30")
    except Exception:
        return jsonify({"status": "error", "message": "Invalid date format"})

    stocks_to_scan = bot_state["fno_stocks"]
    if not stocks_to_scan:
        return jsonify({"status": "error", "message": "F&O universe loading..."})

    filtered_results = []
    for item in stocks_to_scan:
        if bot_state["scan_cancelled"]:
            log("Manual Scan stopped by user.")
            break

        time.sleep(0.04)
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

    return jsonify({
        "status": "success",
        "date": selected_date,
        "count": len(filtered_results),
        "cancelled": bot_state["scan_cancelled"],
        "results": filtered_results
    })

@app.route('/api/stop-manual-scan', methods=['POST'])
def stop_manual_scan():
    bot_state["scan_cancelled"] = True
    return jsonify({"status": "success", "message": "Scan stopped."})

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify({
        "logged_in": bot_state["is_logged_in"],
        "is_logged_in": bot_state["is_logged_in"],
        "broker_connected": bot_state["is_logged_in"],
        "is_market_live": bot_state["is_market_live"],
        "angel_status": "CONNECTED" if bot_state["is_logged_in"] else "WAITING LOGIN",
        "engine_running": bot_state["engine_running"],
        "trading_mode": bot_state["trading_mode"],
        "max_trades": bot_state["max_trades"],
        "rr_ratio": bot_state["rr_ratio"],
        "cutoff_time": bot_state["cutoff_time"],
        "risk_amount": bot_state["risk_amount"],
        "trades_executed_today": bot_state["trades_executed_today"],
        "stocks_loaded_count": len(bot_state["fno_stocks"]),
        "status": bot_state["status_log"],
        "system_logs": bot_state["system_logs"],
        "total_pnl": bot_state["total_pnl"],
        "market_indices": bot_state["market_indices"],
        "market_stats": bot_state["market_stats"],
        "market_movers": bot_state["market_stats"],
        "c1_candidates": bot_state["c1_candidates"],
        "pending_orders": bot_state["pending_orders"],
        "active_trades": bot_state["active_trades"],
        "trade_history": bot_state["trade_history"]
    })

def place_live_slm_order(symbol, token, side, qty, trigger_price):
    try:
        order_params = {
            "variety": "STOPLOSS",
            "tradingsymbol": symbol,
            "symboltoken": str(token),
            "transactiontype": side,
            "exchange": "NSE",
            "ordertype": "STOPLOSS_MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "triggerprice": str(round(float(trigger_price), 2)),
            "quantity": str(qty)
        }
        res = bot_state["smart_api"].placeOrder(order_params)
        log(f"LIVE SL-M ORDER: {side} {qty} {symbol} Trigger@{trigger_price} | Res: {res}")
        if res and res.get("data") and res["data"].get("orderid"):
            return res["data"]["orderid"]
        return "LIVE_SIMULATED_ID"
    except Exception as e:
        log(f"LIVE SL-M ORDER ERROR: {e}")
        return None

def cancel_live_order(order_id):
    if not order_id or str(order_id).startswith("PAPER_"):
        return True
    try:
        res = bot_state["smart_api"].cancelOrder(order_id, "STOPLOSS")
        log(f"CANCELLED BROKER ORDER ID: {order_id} | Res: {res}")
        return True
    except Exception as e:
        log(f"CANCEL BROKER ORDER ERROR: {e}")
        return False

def place_live_exit_order(symbol, token, side, qty):
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
        return bot_state["smart_api"].placeOrder(order_params)
    except Exception as e:
        log(f"LIVE EXIT ERROR: {e}")
        return None

# =========================================================================
# CONTINUOUS SL-M EXECUTION & C1 INVALIDATION LOOP (TILL CUTOFF)
# =========================================================================
def background_scanner():
    c1_scanned = False

    while bot_state["is_logged_in"]:
        if not bot_state["engine_running"]:
            time.sleep(1)
            continue

        now_ist = get_ist_now()
        now_time = now_ist.time()

        cutoff_parts = [int(x) for x in bot_state["cutoff_time"].split(":")]
        cutoff_time_obj = datetime.time(cutoff_parts[0], cutoff_parts[1])

        # Past cutoff: cancel remaining pending orders
        if now_time >= cutoff_time_obj:
            if bot_state["pending_orders"]:
                for po in bot_state["pending_orders"]:
                    if po["status"] == "PENDING":
                        cancel_live_order(po.get("order_id"))
                        po["status"] = "CANCELLED_CUTOFF"
                        log(f"Cutoff Time Hit: Cancelled pending SL-M on {po['symbol']}")
            time.sleep(5)
            continue

        # 09:20 AM - C1 Filter Phase
        if not c1_scanned and now_time >= datetime.time(9, 20, 2):
            log(f"09:20 AM: Scanning all {len(bot_state['fno_stocks'])} F&O stocks for 5x Volume + PDH/PDL Breakout...")
            candidates = []

            for item in bot_state["fno_stocks"]:
                time.sleep(0.04)
                df = fetch_candles(item["token"])
                if df is not None and len(df) >= 22:
                    avg_vol = df.iloc[-22:-2]["volume"].mean()
                    c1_candle = df.iloc[-2]
                    c1_vol = float(c1_candle["volume"])
                    c1_close = float(c1_candle["close"])
                    c1_high = float(c1_candle["high"])
                    c1_low = float(c1_candle["low"])
                    pdh = item.get("pdh", 0)
                    pdl = item.get("pdl", 0)

                    if avg_vol > 0 and (c1_vol >= 5 * avg_vol):
                        bias = None
                        if pdh > 0 and c1_close > pdh:
                            bias = "BULLISH_PDH_BREAKOUT"
                        elif pdl > 0 and c1_close < pdl:
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
                                "ratio": round(c1_vol / avg_vol, 2),
                                "order_state": "READY"
                            })
                            log(f"Setup Qualified: {item['symbol']} ({round(c1_vol/avg_vol, 2)}x Vol) [{bias}]")

            bot_state["c1_candidates"] = candidates
            log(f"C1 Scan Complete: {len(candidates)} candidate(s) ready for continuous SL-M engine.")
            c1_scanned = True

        # CONTINUOUS ENGINE: 09:25 AM to CUTOFF TIME
        if c1_scanned and now_time >= datetime.time(9, 25, 2):
            active_open_count = len([t for t in bot_state["active_trades"] if t["status"] == "OPEN"])
            pending_count = len([p for p in bot_state["pending_orders"] if p["status"] == "PENDING"])

            # Place SL-M orders on valid candidates up to max_trades
            if (active_open_count + pending_count) < bot_state["max_trades"]:
                for cand in bot_state["c1_candidates"]:
                    if (active_open_count + pending_count) >= bot_state["max_trades"]:
                        break

                    if cand.get("order_state") != "READY":
                        continue

                    # Validate C2 Candle
                    df = fetch_candles(cand["token"])
                    if df is not None and len(df) >= 2:
                        c2 = df.iloc[-1]
                        c2_high = float(c2["high"])
                        c2_low = float(c2["low"])
                        c2_close = float(c2["close"])
                        c1_h = cand["c1_high"]
                        c1_l = cand["c1_low"]

                        side = None
                        slm_trigger = 0.0
                        sl = 0.0

                        if cand["bias"] == "BULLISH_PDH_BREAKOUT":
                            if c2_close > c1_h:
                                side = "BUY"
                                slm_trigger = c2_high
                                sl = c2_low
                            elif c2_high <= c1_h and c2_low >= c1_l:
                                side = "BUY"
                                slm_trigger = c1_h
                                sl = c2_low

                        elif cand["bias"] == "BEARISH_PDL_BREAKDOWN":
                            if c2_close < c1_l:
                                side = "SELL"
                                slm_trigger = c2_low
                                sl = c2_high
                            elif c2_high <= c1_h and c2_low >= c1_l:
                                side = "SELL"
                                slm_trigger = c1_l
                                sl = c2_high

                        if side and slm_trigger > 0 and sl > 0:
                            risk_pts = abs(slm_trigger - sl)
                            qty = calculate_quantity(bot_state["risk_amount"], slm_trigger, sl)
                            target_mult = bot_state["rr_ratio"]
                            final_target = round(slm_trigger + (target_mult * risk_pts) if side == "BUY" else slm_trigger - (target_mult * risk_pts), 2)

                            mode = bot_state["trading_mode"]
                            order_id = f"PAPER_{int(time.time()*1000)}"
                            if mode == "LIVE":
                                order_id = place_live_slm_order(cand["symbol"], cand["token"], side, qty, slm_trigger)

                            cand["order_state"] = "PENDING_PLACED"

                            bot_state["pending_orders"].append({
                                "id": len(bot_state["pending_orders"]) + 1,
                                "order_id": order_id,
                                "symbol": cand["symbol"],
                                "token": cand["token"],
                                "side": side,
                                "trigger_price": slm_trigger,
                                "sl": sl,
                                "orig_sl": sl,
                                "c1_high": c1_h,
                                "c1_low": c1_l,
                                "target": final_target,
                                "rr_ratio": target_mult,
                                "qty": qty,
                                "status": "PENDING",
                                "mode": mode,
                                "time": get_ist_now().strftime("%I:%M:%S %p")
                            })
                            pending_count += 1
                            log(f"SL-M Order Armed [{mode}]: {side} {cand['symbol']} Trigger@{slm_trigger} (C1 Inval Level: {'<' + str(c1_l) if side == 'BUY' else '>' + str(c1_h)})")

            # Check Invalidation & Fill on all PENDING orders
            for po in bot_state["pending_orders"]:
                if po["status"] != "PENDING":
                    continue

                try:
                    res = bot_state["smart_api"].ltpData("NSE", po["symbol"], str(po["token"]))
                    if res and res.get("status") and res.get("data"):
                        ltp = float(res["data"]["ltp"])

                        # CONDITION 1: INVALIDATION CHECK (BREAKS C1 LOW FOR BUY / C1 HIGH FOR SELL)
                        is_invalid = False
                        if po["side"] == "BUY" and ltp < po["c1_low"]:
                            is_invalid = True
                            reason = f"LTP (₹{ltp}) broke C1 Low (₹{po['c1_low']})"
                        elif po["side"] == "SELL" and ltp > po["c1_high"]:
                            is_invalid = True
                            reason = f"LTP (₹{ltp}) broke C1 High (₹{po['c1_high']})"

                        if is_invalid:
                            po["status"] = "CANCELLED_INVALID"
                            cancel_live_order(po.get("order_id"))
                            # Free candidate state
                            for c in bot_state["c1_candidates"]:
                                if c["symbol"] == po["symbol"]:
                                    c["order_state"] = "INVALIDATED"
                            log(f"⚠️ SETUP INVALIDATED: {po['symbol']} cancelled! Reason: {reason}. Rotating to next stock...")
                            continue

                        # CONDITION 2: TRIGGER HIT CHECK (MOVES TO ACTIVE TRADES)
                        triggered = False
                        if po["side"] == "BUY" and ltp >= po["trigger_price"]:
                            triggered = True
                        elif po["side"] == "SELL" and ltp <= po["trigger_price"]:
                            triggered = True

                        if triggered:
                            po["status"] = "TRIGGERED"
                            bot_state["active_trades"].append({
                                "id": len(bot_state["active_trades"]) + 1,
                                "symbol": po["symbol"],
                                "token": po["token"],
                                "side": po["side"],
                                "trigger_price": po["trigger_price"],
                                "entry": ltp,
                                "sl": po["sl"],
                                "orig_sl": po["orig_sl"],
                                "target": po["target"],
                                "rr_ratio": po["rr_ratio"],
                                "qty": po["qty"],
                                "remaining_qty": po["qty"],
                                "half_booked": False,
                                "ltp": ltp,
                                "pnl": 0.0,
                                "status": "OPEN",
                                "mode": po["mode"],
                                "time": get_ist_now().strftime("%I:%M:%S %p")
                            })
                            bot_state["trades_executed_today"] += 1
                            log(f"⚡ SL-M TRIGGERED & FILLED: {po['side']} {po['symbol']} at ₹{ltp}")
                except Exception:
                    pass
                time.sleep(0.04)

        time.sleep(1)

# =========================================================================
# ACCURATE REALTIME MOVERS & BUZZERS (STRICTLY F&O CASH ONLY)
# =========================================================================
def update_fno_market_movers_and_buzzers():
    if not bot_state["is_logged_in"] or not bot_state["fno_stocks"]:
        return

    fno_perf = []

    for s in bot_state["fno_stocks"]:
        try:
            res = bot_state["smart_api"].ltpData("NSE", s["symbol"], str(s["token"]))
            if res and res.get("status") and res.get("data"):
                d = res["data"]
                ltp = float(d.get("ltp") or 0.0)
                close = float(d.get("close") or s.get("prev_close") or ltp)
                
                live_vol = int(d.get("trade_volume") or d.get("volume") or 0)
                effective_vol = live_vol if live_vol > 0 else int(s.get("last_daily_vol") or 0)
                avg_vol = float(s.get("avg_5d_vol") or 1.0)
                vol_burst = round(effective_vol / avg_vol, 2) if avg_vol > 0 else 1.0

                if ltp > 0:
                    pchange = round(((ltp - close) / close) * 100, 2) if close > 0 else 0.0
                    clean_sym = s["symbol"].replace("-EQ", "")
                    fno_perf.append({
                        "symbol": clean_sym,
                        "ltp": ltp,
                        "pchange": pchange,
                        "volume": effective_vol,
                        "burst": vol_burst
                    })
        except Exception:
            pass
        time.sleep(0.005)

    if len(fno_perf) >= 5:
        df_p = pd.DataFrame(fno_perf)
        bot_state["market_stats"]["top_gainers"] = df_p.sort_values(by="pchange", ascending=False).head(10).to_dict('records')
        bot_state["market_stats"]["top_losers"] = df_p.sort_values(by="pchange", ascending=True).head(10).to_dict('records')
        valid_buzzers = df_p[df_p["volume"] > 0]
        if not valid_buzzers.empty:
            bot_state["market_stats"]["volume_buzzers"] = valid_buzzers.sort_values(by="burst", ascending=False).head(10).to_dict('records')

def market_data_monitor():
    last_stats_check = 0

    while bot_state["is_logged_in"]:
        now_ts = time.time()
        now_ist = get_ist_now()

        is_weekday = (now_ist.weekday() < 5)
        is_time_window = (datetime.time(9, 15) <= now_ist.time() <= datetime.time(15, 30))

        try:
            n_res = bot_state["smart_api"].ltpData("NSE", "NIFTY", "99926000")
            if n_res and n_res.get("data"):
                ltp = float(n_res["data"]["ltp"])
                close = float(n_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["NIFTY"] = {"ltp": ltp, "change": change, "pchange": pchange}
                bot_state["is_market_live"] = (is_weekday and is_time_window)
            else:
                bot_state["is_market_live"] = False

            s_res = bot_state["smart_api"].ltpData("BSE", "SENSEX", "99919000")
            if s_res and s_res.get("data"):
                ltp = float(s_res["data"]["ltp"])
                close = float(s_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["SENSEX"] = {"ltp": ltp, "change": change, "pchange": pchange}
        except Exception:
            bot_state["is_market_live"] = False

        if now_ts - last_stats_check > 10:
            last_stats_check = now_ts
            update_fno_market_movers_and_buzzers()

        # Monitoring Active Positions
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
                        if not trade["half_booked"] and ltp >= (trade["entry"] + 2 * risk_unit):
                            half_qty = max(1, trade["remaining_qty"] // 2)
                            trade["remaining_qty"] -= half_qty
                            trade["half_booked"] = True
                            trade["sl"] = trade["entry"]
                            log(f"1:2 Hit on {trade['symbol']}! Booked 50%. SL trailed to Cost.")

                        if ltp <= trade["sl"]:
                            trade["status"] = "SL / TRAIL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_exit_order(trade["symbol"], trade["token"], "SELL", trade["remaining_qty"])
                            record_trade_history(trade, ltp)
                        elif ltp >= trade["target"]:
                            trade["status"] = "FULL TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_exit_order(trade["symbol"], trade["token"], "SELL", trade["remaining_qty"])
                            record_trade_history(trade, ltp)

                    else:
                        trade["pnl"] = round((trade["entry"] - ltp) * trade["remaining_qty"], 2)
                        if not trade["half_booked"] and ltp <= (trade["entry"] - 2 * risk_unit):
                            half_qty = max(1, trade["remaining_qty"] // 2)
                            trade["remaining_qty"] -= half_qty
                            trade["half_booked"] = True
                            trade["sl"] = trade["entry"]
                            log(f"1:2 Hit on {trade['symbol']}! Booked 50%. SL trailed to Cost.")

                        if ltp >= trade["sl"]:
                            trade["status"] = "SL / TRAIL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_exit_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                            record_trade_history(trade, ltp)
                        elif ltp <= trade["target"]:
                            trade["status"] = "FULL TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_exit_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                            record_trade_history(trade, ltp)
            except Exception:
                pass
            time.sleep(0.08)

        bot_state["total_pnl"] = round(sum(t["pnl"] for t in bot_state["active_trades"] if t["status"] == "OPEN"), 2)
        time.sleep(1)

def record_trade_history(trade, exit_price):
    bot_state["trade_history"].append({
        "time": get_ist_now().strftime("%I:%M:%S %p"),
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
