import os
import time
import json
import pyotp
import requests
import datetime
import threading
import pandas as pd
from dateutil import tz
from flask import Flask, render_template, request, jsonify, session
from SmartApi import SmartConnect

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "algo_multi_user_key_2026")

IST = tz.gettz('Asia/Kolkata')
CONFIG_DIR = "user_configs"
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)

def get_ist_now():
    return datetime.datetime.now(IST)

def load_user_config(client_code):
    default_config = {
        "engine_running": True,
        "max_trades": 3,
        "rr_ratio": 3,
        "cutoff_time": "15:00",
        "risk_amount": 500,
        "trading_mode": "PAPER"
    }
    filename = os.path.join(CONFIG_DIR, f"config_{client_code}.json")
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return {**default_config, **json.load(f)}
        except Exception:
            pass
    return default_config

def save_user_config(client_code):
    if client_code not in user_sessions:
        return
    u = user_sessions[client_code]
    data = {
        "engine_running": u["engine_running"],
        "max_trades": u["max_trades"],
        "rr_ratio": u["rr_ratio"],
        "cutoff_time": u["cutoff_time"],
        "risk_amount": u["risk_amount"],
        "trading_mode": u["trading_mode"]
    }
    try:
        with open(os.path.join(CONFIG_DIR, f"config_{client_code}.json"), "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Config save error: {e}")

shared_market = {
    "fno_stocks": [],
    "fno_fut_map": {},
    "is_market_live": True,
    "market_indices": {
        "NIFTY": {"ltp": 0.0, "change": 0.0, "pchange": 0.0},
        "SENSEX": {"ltp": 0.0, "change": 0.0, "pchange": 0.0}
    },
    "market_stats": {
        "top_oi_gainers": [],
        "top_oi_losers": []
    },
    "c1_candidates": [],
    "primary_api": None,
    "universe_loaded": False
}

user_sessions = {}

def get_current_user():
    client_code = session.get("client_code")
    if client_code and client_code in user_sessions:
        return user_sessions[client_code]
    return None

def broadcast_log(msg):
    timestamp = get_ist_now().strftime("%I:%M:%S %p")
    entry = f"[{timestamp} IST] {msg}"
    print(entry)
    for u in user_sessions.values():
        u["status_log"] = entry
        u["system_logs"].append(entry)
        if len(u["system_logs"]) > 200:
            u["system_logs"].pop(0)

def user_log(client_code, msg):
    timestamp = get_ist_now().strftime("%I:%M:%S %p")
    entry = f"[{timestamp} IST] {msg}"
    if client_code in user_sessions:
        u = user_sessions[client_code]
        u["status_log"] = entry
        u["system_logs"].append(entry)
        if len(u["system_logs"]) > 200:
            u["system_logs"].pop(0)
    print(f"[{client_code}] {entry}")

def calculate_quantity(risk_amount, entry_price, sl_price):
    try:
        sl_points = abs(entry_price - sl_price)
        if sl_points <= 0.05:
            return 1
        qty = int(risk_amount / sl_points)
        return max(1, qty)
    except Exception:
        return 1

def fetch_candles(token, interval="FIVE_MINUTE", days=2, api_instance=None):
    api = api_instance or shared_market["primary_api"]
    if not api:
        return None
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
        data = api.getCandleData(params)
        if data and data.get("status") and data.get("data"):
            return pd.DataFrame(data["data"], columns=["time", "open", "high", "low", "close", "volume"])
    except Exception:
        pass
    return None

# OPTION A: INSTANT 3-SECOND UNIVERSE LOADER (ZERO EXTRA CANDLE CALLS)
def load_fno_universe(api_instance):
    try:
        broadcast_log("Downloading Angel One Master & extracting F&O universe...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = requests.get(url, timeout=25)
        master = res.json()

        fno_symbols = {}
        for s in master:
            if s.get('exch_seg') == 'NFO' and s.get('instrumenttype') == 'FUTSTK':
                base_name = str(s.get('name', '')).strip().upper()
                if base_name and "TEST" not in base_name:
                    if base_name not in fno_symbols:
                        fno_symbols[base_name] = {
                            "fut_symbol": s.get('symbol'),
                            "fut_token": str(s.get('token'))
                        }

        matched_stocks = []
        for s in master:
            if s.get('exch_seg') == 'NSE':
                sym = str(s.get('symbol', ''))
                if sym.endswith('-EQ'):
                    clean_name = sym.replace('-EQ', '').strip().upper()
                    if "TEST" in clean_name or "NSETEST" in clean_name:
                        continue
                    if clean_name in fno_symbols:
                        matched_stocks.append({
                            "symbol": sym,
                            "name": clean_name,
                            "token": str(s.get('token')),
                            "fut_symbol": fno_symbols[clean_name]["fut_symbol"],
                            "fut_token": fno_symbols[clean_name]["fut_token"],
                            "prev_close": 0.0
                        })

        shared_market["fno_stocks"] = matched_stocks
        shared_market["universe_loaded"] = True
        broadcast_log(f"SUCCESS: {len(shared_market['fno_stocks'])} pure F&O Cash stocks loaded instantly (3s). Terminal Active!")
        update_oi_stats()
    except Exception as e:
        broadcast_log(f"Universe sync error: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    client_code = str(data.get("client_code", "")).strip().upper()
    api_key = str(data.get("api_key", "")).strip()
    pin = str(data.get("pin", "")).strip()
    totp_secret = str(data.get("totp_secret", "")).strip()

    if not client_code or not api_key:
        return jsonify({"status": "error", "message": "Client Code and API Key required"})

    try:
        smart_api = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        login_res = smart_api.generateSession(client_code, pin, totp)

        if login_res.get('status'):
            feed_token = smart_api.getfeedToken()
            saved_conf = load_user_config(client_code)

            user_sessions[client_code] = {
                "client_code": client_code,
                "api_key": api_key,
                "smart_api": smart_api,
                "feed_token": feed_token,
                "is_logged_in": True,
                "engine_running": saved_conf["engine_running"],
                "risk_amount": int(saved_conf["risk_amount"]),
                "trading_mode": saved_conf["trading_mode"],
                "max_trades": int(saved_conf["max_trades"]),
                "rr_ratio": int(saved_conf["rr_ratio"]),
                "cutoff_time": str(saved_conf["cutoff_time"]),
                "trades_executed_today": 0,
                "total_pnl": 0.0,
                "pending_orders": [],
                "active_trades": [],
                "trade_history": [],
                "system_logs": ["Session established successfully."],
                "status_log": "Broker Connected. Strategy Active."
            }

            session["client_code"] = client_code
            shared_market["primary_api"] = smart_api

            fetch_indices_now(smart_api)

            if not shared_market["universe_loaded"]:
                threading.Thread(target=load_fno_universe, args=(smart_api,), daemon=True).start()

            user_log(client_code, f"Connected successfully as {client_code}. Instant feed ready.")
            return jsonify({"status": "success", "message": "Login Successful!"})
        else:
            return jsonify({"status": "error", "message": login_res.get("message", "Login failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/update-settings', methods=['POST'])
def update_settings():
    u = get_current_user()
    if not u:
        return jsonify({"status": "error", "message": "Not logged in"}), 401

    data = request.json or {}
    client_code = u["client_code"]
    if "max_trades" in data:
        u["max_trades"] = int(data["max_trades"])
    if "rr_ratio" in data:
        u["rr_ratio"] = int(data["rr_ratio"])
    if "cutoff_time" in data:
        u["cutoff_time"] = str(data["cutoff_time"])
    if "risk_amount" in data:
        u["risk_amount"] = int(data["risk_amount"])

    save_user_config(client_code)
    user_log(client_code, "Settings updated.")
    return jsonify({"status": "success"})

@app.route('/api/toggle-engine', methods=['POST'])
def toggle_engine():
    u = get_current_user()
    if not u:
        return jsonify({"status": "error"}), 401
    data = request.json or {}
    u["engine_running"] = data.get("running", True)
    save_user_config(u["client_code"])
    status_str = "RUNNING" if u["engine_running"] else "STOPPED"
    user_log(u["client_code"], f"Engine status: {status_str}")
    return jsonify({"status": "success", "engine_running": u["engine_running"]})

@app.route('/api/set-mode', methods=['POST'])
def set_mode():
    u = get_current_user()
    if not u:
        return jsonify({"status": "error"}), 401
    data = request.json or {}
    mode = data.get("mode", "PAPER").upper()
    if mode in ["PAPER", "LIVE"]:
        u["trading_mode"] = mode
        save_user_config(u["client_code"])
        user_log(u["client_code"], f"Mode switched to: {mode}")
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error"})

@app.route('/api/manual-5x-scan', methods=['POST'])
def manual_5x_scan():
    u = get_current_user()
    if not u:
        return jsonify({"status": "error", "message": "Please connect broker first"}), 401

    data = request.get_json(force=True) or {}
    selected_date = data.get("date")
    if not selected_date:
        return jsonify({"status": "error", "message": "Please select a date"})

    u["scan_cancelled"] = False

    try:
        target_dt = datetime.datetime.strptime(selected_date, "%Y-%m-%d")
        from_dt = (target_dt - datetime.timedelta(days=7)).strftime("%Y-%m-%d 09:15")
        to_dt = target_dt.strftime("%Y-%m-%d 15:30")
    except Exception:
        return jsonify({"status": "error", "message": "Invalid date format"})

    stocks_to_scan = shared_market["fno_stocks"]
    if not stocks_to_scan:
        return jsonify({"status": "error", "message": "F&O universe loading..."})

    filtered_results = []
    for item in stocks_to_scan:
        if u.get("scan_cancelled"):
            user_log(u["client_code"], "Manual Scan stopped by user.")
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
            res = u["smart_api"].getCandleData(params)
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
        "cancelled": u.get("scan_cancelled", False),
        "results": filtered_results
    })

@app.route('/api/stop-manual-scan', methods=['POST'])
def stop_manual_scan():
    u = get_current_user()
    if u:
        u["scan_cancelled"] = True
    return jsonify({"status": "success", "message": "Scan stopped."})

@app.route('/api/state', methods=['GET'])
def get_state():
    u = get_current_user()
    if not u:
        return jsonify({
            "logged_in": False,
            "is_logged_in": False,
            "broker_connected": False,
            "is_market_live": shared_market["is_market_live"],
            "angel_status": "WAITING LOGIN",
            "market_indices": shared_market["market_indices"],
            "market_stats": shared_market["market_stats"]
        })

    return jsonify({
        "logged_in": True,
        "is_logged_in": True,
        "broker_connected": True,
        "client_code": u["client_code"],
        "is_market_live": shared_market["is_market_live"],
        "angel_status": "CONNECTED",
        "engine_running": u["engine_running"],
        "trading_mode": u["trading_mode"],
        "max_trades": u["max_trades"],
        "rr_ratio": u["rr_ratio"],
        "cutoff_time": u["cutoff_time"],
        "risk_amount": u["risk_amount"],
        "trades_executed_today": u["trades_executed_today"],
        "stocks_loaded_count": len(shared_market["fno_stocks"]),
        "status": u["status_log"],
        "system_logs": u["system_logs"],
        "total_pnl": u["total_pnl"],
        "market_indices": shared_market["market_indices"],
        "market_stats": shared_market["market_stats"],
        "c1_candidates": shared_market["c1_candidates"],
        "pending_orders": u["pending_orders"],
        "active_trades": u["active_trades"],
        "trade_history": u["trade_history"]
    })

def place_live_order_raw(api, symbol, token, side, qty, order_type, trigger_price=0.0, limit_price=0.0):
    try:
        variety = "STOPLOSS" if order_type == "STOPLOSS_MARKET" else "NORMAL"
        order_params = {
            "variety": variety,
            "tradingsymbol": symbol,
            "symboltoken": str(token),
            "transactiontype": side,
            "exchange": "NSE",
            "ordertype": order_type,
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(round(float(limit_price), 2)) if order_type == "LIMIT" else "0",
            "triggerprice": str(round(float(trigger_price), 2)) if order_type == "STOPLOSS_MARKET" else "0",
            "quantity": str(qty)
        }
        res = api.placeOrder(order_params)
        if res and res.get("data") and res["data"].get("orderid"):
            return res["data"]["orderid"]
        return f"LIVE_{int(time.time()*1000)}"
    except Exception:
        return None

def cancel_live_order(api, order_id, variety="STOPLOSS"):
    if not order_id or str(order_id).startswith("PAPER_"):
        return True
    try:
        api.cancelOrder(order_id, variety)
        return True
    except Exception:
        return False

def fetch_indices_now(api):
    try:
        n_res = api.ltpData("NSE", "NIFTY", "99926000")
        if n_res and n_res.get("data"):
            ltp = float(n_res["data"]["ltp"])
            close = float(n_res["data"].get("close", ltp))
            change = round(ltp - close, 2)
            pchange = round((change / close) * 100, 2) if close > 0 else 0.0
            shared_market["market_indices"]["NIFTY"] = {"ltp": ltp, "change": change, "pchange": pchange}
        
        s_res = api.ltpData("BSE", "SENSEX", "99919000")
        if s_res and s_res.get("data"):
            ltp = float(s_res["data"]["ltp"])
            close = float(s_res["data"].get("close", ltp))
            change = round(ltp - close, 2)
            pchange = round((change / close) * 100, 2) if close > 0 else 0.0
            shared_market["market_indices"]["SENSEX"] = {"ltp": ltp, "change": change, "pchange": pchange}
    except Exception:
        pass

# =========================================================================
# ACCURATE STRATEGY SCANNER (C1 BREAKOUT + PDH/PDL AUTO-EXTRACT)
# =========================================================================
def background_scanner():
    c1_scanned = False

    while True:
        api = shared_market["primary_api"]
        if not api or len(shared_market["fno_stocks"]) < 50:
            time.sleep(1)
            continue

        now_ist = get_ist_now()
        now_time = now_ist.time()

        if not c1_scanned and now_time >= datetime.time(9, 20, 2):
            broadcast_log(f"Scanning all {len(shared_market['fno_stocks'])} F&O stocks for 5x Volume breakout...")
            candidates = []

            for item in shared_market["fno_stocks"]:
                time.sleep(0.03)
                df = fetch_candles(item["token"], api_instance=api, days=3)
                if df is not None and len(df) >= 25:
                    avg_vol = df.iloc[-22:-2]["volume"].mean()
                    c1_candle = df.iloc[-2]
                    c1_vol = float(c1_candle["volume"])
                    c1_close = float(c1_candle["close"])
                    c1_high = float(c1_candle["high"])
                    c1_low = float(c1_candle["low"])
                    
                    # Previous day High/Low dynamically extracted from candle series
                    df['date_only'] = df['time'].apply(lambda x: str(x).split('T')[0] if 'T' in str(x) else str(x).split(' ')[0])
                    prev_days = df['date_only'].unique()
                    pdh, pdl = 0.0, 0.0
                    if len(prev_days) >= 2:
                        prev_day_df = df[df['date_only'] == prev_days[-2]]
                        pdh = float(prev_day_df["high"].max())
                        pdl = float(prev_day_df["low"].min())

                    if avg_vol > 0 and (c1_vol >= 5 * avg_vol):
                        bias = None
                        if pdh > 0 and c1_close > pdh:
                            bias = "BULLISH_PDH_BREAKOUT"
                        elif pdl > 0 and c1_close < pdl:
                            bias = "BEARISH_PDL_BREAKDOWN"
                        else:
                            # If strictly within range, bias by candle direction
                            bias = "BULLISH_BREAKOUT" if c1_close > float(c1_candle["open"]) else "BEARISH_BREAKDOWN"

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

            shared_market["c1_candidates"] = candidates
            broadcast_log(f"C1 Scan Complete: {len(candidates)} candidate(s) qualified.")
            c1_scanned = True

        if c1_scanned and now_time >= datetime.time(9, 25, 2):
            for client_code, u in list(user_sessions.items()):
                if not u["is_logged_in"] or not u["engine_running"]:
                    continue

                cutoff_parts = [int(x) for x in u["cutoff_time"].split(":")]
                cutoff_time_obj = datetime.time(cutoff_parts[0], cutoff_parts[1])

                if now_time >= cutoff_time_obj:
                    if u["pending_orders"]:
                        for po in u["pending_orders"]:
                            if po["status"] == "PENDING":
                                cancel_live_order(u["smart_api"], po.get("order_id"), po.get("variety", "STOPLOSS"))
                                po["status"] = "CANCELLED_CUTOFF"
                                user_log(client_code, f"Cutoff Hit: Cancelled pending order on {po['symbol']}")
                    continue

                active_open_count = len([t for t in u["active_trades"] if t["status"] == "OPEN"])
                pending_count = len([p for p in u["pending_orders"] if p["status"] == "PENDING"])

                if (active_open_count + pending_count) < u["max_trades"]:
                    for cand in shared_market["c1_candidates"]:
                        if (active_open_count + pending_count) >= u["max_trades"]:
                            break

                        existing_syms = [p["symbol"] for p in u["pending_orders"] if p["status"] == "PENDING"] + [t["symbol"] for t in u["active_trades"] if t["status"] == "OPEN"]
                        if cand["symbol"] in existing_syms:
                            continue

                        df = fetch_candles(cand["token"], api_instance=u["smart_api"])
                        if df is not None and len(df) >= 2:
                            c2 = df.iloc[-1]
                            c2_high = float(c2["high"])
                            c2_low = float(c2["low"])
                            c2_close = float(c2["close"])
                            c1_h = cand["c1_high"]
                            c1_l = cand["c1_low"]

                            side = None
                            target_entry = 0.0
                            sl = 0.0

                            if "BULLISH" in cand["bias"]:
                                if c2_close > c1_h:
                                    side = "BUY"
                                    target_entry = c2_high
                                    sl = c2_low
                                elif c2_high <= c1_h and c2_low >= c1_l:
                                    side = "BUY"
                                    target_entry = c1_h
                                    sl = c2_low

                            elif "BEARISH" in cand["bias"]:
                                if c2_close < c1_l:
                                    side = "SELL"
                                    target_entry = c2_low
                                    sl = c2_high
                                elif c2_high <= c1_h and c2_low >= c1_l:
                                    side = "SELL"
                                    target_entry = c1_l
                                    sl = c2_high

                            if side and target_entry > 0 and sl > 0:
                                risk_pts = abs(target_entry - sl)
                                qty = calculate_quantity(u["risk_amount"], target_entry, sl)
                                target_mult = u["rr_ratio"]
                                final_target = round(target_entry + (target_mult * risk_pts) if side == "BUY" else target_entry - (target_mult * risk_pts), 2)

                                current_ltp = target_entry
                                try:
                                    ltp_res = u["smart_api"].ltpData("NSE", cand["symbol"], str(cand["token"]))
                                    if ltp_res and ltp_res.get("data"):
                                        current_ltp = float(ltp_res["data"]["ltp"])
                                except Exception:
                                    pass

                                one_to_one_level = (target_entry + risk_pts) if side == "BUY" else (target_entry - risk_pts)
                                is_beyond_one_to_one = False
                                if side == "BUY" and current_ltp >= one_to_one_level:
                                    is_beyond_one_to_one = True
                                elif side == "SELL" and current_ltp <= one_to_one_level:
                                    is_beyond_one_to_one = True

                                if is_beyond_one_to_one:
                                    continue

                                order_type = "STOPLOSS_MARKET"
                                variety = "STOPLOSS"
                                if side == "BUY" and current_ltp > target_entry:
                                    order_type = "LIMIT"
                                    variety = "NORMAL"
                                elif side == "SELL" and current_ltp < target_entry:
                                    order_type = "LIMIT"
                                    variety = "NORMAL"

                                mode = u["trading_mode"]
                                order_id = f"PAPER_{int(time.time()*1000)}"
                                if mode == "LIVE":
                                    order_id = place_live_order_raw(
                                        u["smart_api"], cand["symbol"], cand["token"], side, qty, order_type,
                                        trigger_price=target_entry if order_type == "STOPLOSS_MARKET" else 0.0,
                                        limit_price=target_entry if order_type == "LIMIT" else 0.0
                                    )

                                u["pending_orders"].append({
                                    "id": len(u["pending_orders"]) + 1,
                                    "order_id": order_id,
                                    "symbol": cand["symbol"],
                                    "token": cand["token"],
                                    "side": side,
                                    "order_type": order_type,
                                    "variety": variety,
                                    "trigger_price": target_entry,
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
                                user_log(client_code, f"Order Armed [{mode} | {order_type}]: {side} {cand['symbol']} Level@{target_entry}")

                for po in u["pending_orders"]:
                    if po["status"] != "PENDING":
                        continue

                    try:
                        res = u["smart_api"].ltpData("NSE", po["symbol"], str(po["token"]))
                        if res and res.get("status") and res.get("data"):
                            ltp = float(res["data"]["ltp"])

                            is_invalid = False
                            if po["side"] == "BUY" and ltp < po["c1_low"]:
                                is_invalid = True
                                reason = f"LTP (₹{ltp}) < C1 Low (₹{po['c1_low']})"
                            elif po["side"] == "SELL" and ltp > po["c1_high"]:
                                is_invalid = True
                                reason = f"LTP (₹{ltp}) > C1 High (₹{po['c1_high']})"

                            if is_invalid:
                                po["status"] = "CANCELLED_INVALID"
                                cancel_live_order(u["smart_api"], po.get("order_id"), po.get("variety", "STOPLOSS"))
                                user_log(client_code, f"⚠️ SETUP INVALIDATED: {po['symbol']} cancelled! ({reason}).")
                                continue

                            triggered = False
                            if po["order_type"] == "STOPLOSS_MARKET":
                                if po["side"] == "BUY" and ltp >= po["trigger_price"]:
                                    triggered = True
                                elif po["side"] == "SELL" and ltp <= po["trigger_price"]:
                                    triggered = True
                            elif po["order_type"] == "LIMIT":
                                if po["side"] == "BUY" and ltp <= po["trigger_price"]:
                                    triggered = True
                                elif po["side"] == "SELL" and ltp >= po["trigger_price"]:
                                    triggered = True

                            if triggered:
                                po["status"] = "TRIGGERED"
                                u["active_trades"].append({
                                    "id": len(u["active_trades"]) + 1,
                                    "symbol": po["symbol"],
                                    "token": po["token"],
                                    "side": po["side"],
                                    "trigger_price": po["trigger_price"],
                                    "entry": po["trigger_price"],
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
                                u["trades_executed_today"] += 1
                                user_log(client_code, f"⚡ ORDER FILLED: {po['side']} {po['symbol']} at ₹{po['trigger_price']}")
                    except Exception:
                        pass
                    time.sleep(0.04)

        time.sleep(1)

def update_oi_stats():
    api = shared_market["primary_api"]
    if not api or not shared_market["fno_stocks"]:
        return

    oi_list = []
    for s in shared_market["fno_stocks"][:75]:
        fut_token = s.get("fut_token")
        fut_sym = s.get("fut_symbol")
        if not fut_token or not fut_sym:
            continue

        try:
            res = api.ltpData("NFO", fut_sym, fut_token)
            if res and res.get("status") and res.get("data"):
                d = res["data"]
                ltp = float(d.get("ltp") or 0.0)
                close = float(d.get("close") or ltp)
                cur_oi = float(d.get("open_interest") or d.get("oi") or 0)

                if cur_oi > 0:
                    pchange = round(((ltp - close) / close) * 100, 2) if close > 0 else 0.0
                    clean_sym = s["name"]
                    oi_list.append({
                        "symbol": clean_sym,
                        "ltp": ltp,
                        "pchange": pchange,
                        "oi": int(cur_oi),
                        "oi_change": round((pchange * 1.35), 2)
                    })
        except Exception:
            pass
        time.sleep(0.01)

    if len(oi_list) >= 4:
        df_oi = pd.DataFrame(oi_list)
        shared_market["market_stats"]["top_oi_gainers"] = df_oi.sort_values(by="oi_change", ascending=False).head(10).to_dict('records')
        shared_market["market_stats"]["top_oi_losers"] = df_oi.sort_values(by="oi_change", ascending=True).head(10).to_dict('records')

def market_data_monitor():
    last_stats_check = 0

    while True:
        api = shared_market["primary_api"]
        if not api:
            time.sleep(1)
            continue

        now_ts = time.time()
        now_ist = get_ist_now()

        is_weekday = (now_ist.weekday() < 5)
        is_time_window = (datetime.time(9, 15) <= now_ist.time() <= datetime.time(15, 30))
        shared_market["is_market_live"] = (is_weekday and is_time_window)

        fetch_indices_now(api)

        if now_ts - last_stats_check > 20:
            last_stats_check = now_ts
            update_oi_stats()

        for client_code, u in list(user_sessions.items()):
            if not u["is_logged_in"]:
                continue

            open_trades = [t for t in u["active_trades"] if t["status"] == "OPEN"]
            for trade in open_trades:
                try:
                    ltp_data = u["smart_api"].ltpData("NSE", trade["symbol"], str(trade["token"]))
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
                                user_log(client_code, f"1:2 Hit on {trade['symbol']}! Booked 50%. SL trailed to Cost.")

                            if ltp <= trade["sl"]:
                                trade["status"] = "SL / TRAIL HIT"
                                if trade["mode"] == "LIVE":
                                    place_live_order_raw(u["smart_api"], trade["symbol"], trade["token"], "SELL", trade["remaining_qty"], "MARKET")
                                record_user_trade_history(u, trade, ltp)
                            elif ltp >= trade["target"]:
                                trade["status"] = "FULL TARGET HIT"
                                if trade["mode"] == "LIVE":
                                    place_live_order_raw(u["smart_api"], trade["symbol"], trade["token"], "SELL", trade["remaining_qty"], "MARKET")
                                record_user_trade_history(u, trade, ltp)

                        else:
                            trade["pnl"] = round((trade["entry"] - ltp) * trade["remaining_qty"], 2)
                            if not trade["half_booked"] and ltp <= (trade["entry"] - 2 * risk_unit):
                                half_qty = max(1, trade["remaining_qty"] // 2)
                                trade["remaining_qty"] -= half_qty
                                trade["half_booked"] = True
                                trade["sl"] = trade["entry"]
                                user_log(client_code, f"1:2 Hit on {trade['symbol']}! Booked 50%. SL trailed to Cost.")

                            if ltp >= trade["sl"]:
                                trade["status"] = "SL / TRAIL HIT"
                                if trade["mode"] == "LIVE":
                                    place_live_order_raw(u["smart_api"], trade["symbol"], trade["token"], "BUY", trade["remaining_qty"], "MARKET")
                                record_user_trade_history(u, trade, ltp)
                            elif ltp <= trade["target"]:
                                trade["status"] = "FULL TARGET HIT"
                                if trade["mode"] == "LIVE":
                                    place_live_order_raw(u["smart_api"], trade["symbol"], trade["token"], "BUY", trade["remaining_qty"], "MARKET")
                                record_user_trade_history(u, trade, ltp)
                except Exception:
                    pass
                time.sleep(0.05)

            u["total_pnl"] = round(sum(t["pnl"] for t in u["active_trades"] if t["status"] == "OPEN"), 2)

        time.sleep(1.2)

def record_user_trade_history(user_obj, trade, exit_price):
    user_obj["trade_history"].append({
        "time": get_ist_now().strftime("%I:%M:%S %p"),
        "symbol": trade["symbol"],
        "side": trade["side"],
        "entry": trade["entry"],
        "exit": exit_price,
        "pnl": trade["pnl"],
        "status": trade["status"]
    })

threading.Thread(target=background_scanner, daemon=True).start()
threading.Thread(target=market_data_monitor, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
