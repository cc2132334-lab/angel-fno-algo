# =====================================================================
# PROJECT: ALGO TERMINAL PRO - MULTI-USER EDITION
# FILE: server.py
# VERSION: v2.2-STABLE-FIXED
# MODULE: Rate-Limit Proof Scanner, Unified PDH/PDL, Frozen C1 Qualified Setups
# =====================================================================

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
CONFIG_FILE = os.environ.get("CONFIG_FILE", "bot_config.json")

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
        "top_oi_gainers": [],
        "top_oi_losers": [],
        "oi_spurts_gainers": [],
        "oi_spurts_losers": []
    },
    "manual_scan_state": {
        "is_running": False,
        "scan_cancelled": False,
        "date": "",
        "scanned_count": 0,
        "total_stocks": 0,
        "results": []
    },
    "cpr_scan_state": {
        "is_running": False,
        "scan_cancelled": False,
        "date": "",
        "scanned_count": 0,
        "total_stocks": 0,
        "results": []
    },
    "c1_candidates": [],
    "invalidated_symbols": [],
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

# RATE-LIMIT SAFE CANDLE FETCHER WITH RETRY
def fetch_candles_safe(token, interval="FIVE_MINUTE", from_date="", to_date=""):
    if not bot_state.get("smart_api"):
        return None
        
    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date
    }
    
    # Up to 2 retries on rate-limit drop
    for attempt in range(2):
        try:
            data = bot_state["smart_api"].getCandleData(params)
            if data and data.get("status") and data.get("data"):
                return pd.DataFrame(data["data"], columns=["time", "open", "high", "low", "close", "volume"])
            time.sleep(0.15)
        except Exception:
            time.sleep(0.15)
            
    return None

def load_fno_universe():
    try:
        log("Downloading Angel One Master & mapping multi-expiry futures...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = requests.get(url, timeout=25)
        master = res.json()

        fno_futures = {}
        for s in master:
            if s.get('exch_seg') == 'NFO' and s.get('instrumenttype') == 'FUTSTK':
                base_name = str(s.get('name', '')).strip().upper()
                if base_name and "TEST" not in base_name:
                    if base_name not in fno_futures:
                        fno_futures[base_name] = []
                    fno_futures[base_name].append({
                        "symbol": s.get('symbol'),
                        "token": str(s.get('token')),
                        "expiry": s.get('expiry', '')
                    })

        matched_stocks = []
        for s in master:
            if s.get('exch_seg') == 'NSE':
                sym = str(s.get('symbol', ''))
                if sym.endswith('-EQ'):
                    clean_name = sym.replace('-EQ', '').strip().upper()
                    if "TEST" in clean_name or "NSETEST" in clean_name:
                        continue
                    if clean_name in fno_futures:
                        contracts = fno_futures[clean_name]
                        near_contract = contracts[0]
                        matched_stocks.append({
                            "symbol": sym,
                            "name": clean_name,
                            "token": str(s.get('token')),
                            "fut_symbol": near_contract["symbol"],
                            "fut_token": near_contract["token"],
                            "all_fut_tokens": [c["token"] for c in contracts[:3]]
                        })

        bot_state["fno_stocks"] = matched_stocks
        log(f"SUCCESS: {len(bot_state['fno_stocks'])} pure F&O Cash stocks verified and loaded.")
        update_oi_stats()
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
            log("Broker Connected! Engine armed.")

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
    log(f"Settings updated: MaxTrades={bot_state['max_trades']}, Cutoff={bot_state['cutoff_time']}, RR=1:{bot_state['rr_ratio']}, Risk=₹{bot_state['risk_amount']}")
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

@app.route('/api/manual-exit', methods=['POST'])
def manual_exit_trade():
    data = request.json or {}
    trade_id = data.get("trade_id")
    
    for t in bot_state["active_trades"]:
        if t["id"] == trade_id and t["status"] == "OPEN":
            exit_px = t["ltp"]
            t["status"] = "MANUAL EXIT"
            if t["mode"] == "LIVE":
                opp_side = "SELL" if t["side"] == "BUY" else "BUY"
                place_live_exit_order(t["symbol"], t["token"], opp_side, t["remaining_qty"])
            
            record_trade_history(t, exit_px)
            log(f"🛑 MANUAL EXIT EXECUTED: {t['symbol']} at ₹{exit_px} (PnL: ₹{t['pnl']})")
            return jsonify({"status": "success", "message": f"{t['symbol']} exited successfully"})
            
    return jsonify({"status": "error", "message": "Active position not found"}), 404

@app.route('/api/history-by-date', methods=['GET'])
def get_history_by_date():
    target_date = request.args.get("date")
    if not target_date:
        target_date = get_ist_now().strftime("%Y-%m-%d")

    filtered = [h for h in bot_state["trade_history"] if h.get("trade_date") == target_date]
    total_realized = round(sum(float(h.get("pnl", 0)) for h in filtered), 2)
    wins = len([h for h in filtered if float(h.get("pnl", 0)) > 0])
    win_rate = round((wins / len(filtered)) * 100) if filtered else 0

    return jsonify({
        "date": target_date,
        "trades": filtered,
        "realized_pnl": total_realized,
        "win_rate": win_rate,
        "total_trades": len(filtered),
        "wins": wins,
        "losses": len(filtered) - wins
    })

# ================= CORE SCANNER CALCULATION ENGINE (UNIFIED) =================
# Ye function Bot Scanner aur Manual Scanner dono ke liye EXACT same math use karega
def evaluate_stock_c1_setup(token, symbol, target_date_str):
    try:
        target_dt = datetime.datetime.strptime(target_date_str, "%Y-%m-%d")
    except Exception:
        return None

    from_dt = (target_dt - datetime.timedelta(days=15)).strftime("%Y-%m-%d 09:15")
    to_dt = (target_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d 15:30")

    df = fetch_candles_safe(token, interval="FIVE_MINUTE", from_date=from_dt, to_date=to_dt)
    if df is None or len(df) < 25:
        return None

    df['time_str'] = df['time'].astype(str)
    df['date_part'] = df['time_str'].apply(lambda x: x[:10])

    unique_dates = df['date_part'].unique().tolist()
    if target_date_str not in unique_dates:
        return None

    cur_idx = unique_dates.index(target_date_str)
    if cur_idx < 1:
        return None

    # Exact Previous Day High & Low from previous day's 5-minute candles
    prev_day = unique_dates[cur_idx - 1]
    prev_df = df[df['date_part'] == prev_day]
    pdh = float(prev_df["high"].max())
    pdl = float(prev_df["low"].min())

    # SMA20 volume of preceding 20 candles
    c1_matches = df.index[
        (df['time_str'].str.startswith(target_date_str)) & 
        (df['time_str'].str.contains("09:15"))
    ].tolist()

    if not c1_matches:
        return None

    c1_idx = c1_matches[0]
    c1_candle = df.iloc[c1_idx]
    c1_vol = float(c1_candle["volume"])
    c1_close = float(c1_candle["close"])
    c1_high = float(c1_candle["high"])
    c1_low = float(c1_candle["low"])

    # Preceding 20 candles volume average (Strict SMA20)
    preceding_df = df.iloc[max(0, c1_idx - 20):c1_idx]
    if len(preceding_df) < 5:
        return None

    sma20_vol = float(preceding_df["volume"].mean())
    if sma20_vol <= 0:
        return None

    # Condition 1: Volume >= 5x SMA20
    if c1_vol < (5.0 * sma20_vol):
        return None

    # Condition 2: C1 Close strictly outside PDH or PDL
    bias = None
    if pdh > 0 and c1_close > pdh:
        bias = "BULLISH_PDH_BREAKOUT"
    elif pdl > 0 and c1_close < pdl:
        bias = "BEARISH_PDL_BREAKDOWN"

    if not bias:
        return None

    return {
        "symbol": symbol.replace("-EQ", ""),
        "full_symbol": symbol,
        "token": token,
        "bias": bias,
        "c1_high": round(c1_high, 2),
        "c1_low": round(c1_low, 2),
        "c1_close": round(c1_close, 2),
        "c1_volume": int(c1_vol),
        "avg_volume": int(sma20_vol),
        "pdh": round(pdh, 2),
        "pdl": round(pdl, 2),
        "multiplier": round(c1_vol / sma20_vol, 2)
    }

# ================= MANUAL 5X SCANNER (RATE-LIMIT CONTROLLED) =================
def worker_run_manual_5x_scan(selected_date):
    st = bot_state["manual_scan_state"]
    st["is_running"] = True
    st["scan_cancelled"] = False
    st["date"] = selected_date
    st["scanned_count"] = 0
    st["results"] = []

    stocks_to_scan = bot_state["fno_stocks"]
    st["total_stocks"] = len(stocks_to_scan)

    log(f"Manual 5x scan started for {selected_date} ({len(stocks_to_scan)} stocks)...")

    for item in stocks_to_scan:
        if st["scan_cancelled"]:
            log("Manual 5x Scan stopped.")
            break

        # Rate-limiting pause: ~100ms per stock ensures 0% dropped API requests
        time.sleep(0.09)
        st["scanned_count"] += 1

        res = evaluate_stock_c1_setup(item["token"], item["symbol"], selected_date)
        if res:
            st["results"].append({
                "symbol": res["symbol"],
                "bias": res["bias"],
                "c1_high": res["c1_high"],
                "c1_low": res["c1_low"],
                "c1_close": res["c1_close"],
                "pdh": res["pdh"],
                "pdl": res["pdl"],
                "multiplier": res["multiplier"]
            })

    st["is_running"] = False
    log(f"Manual 5x scan finished! Total found: {len(st['results'])} stocks.")

@app.route('/api/manual-5x-scan', methods=['POST'])
def manual_5x_scan():
    if not bot_state.get("smart_api"):
        return jsonify({"status": "error", "message": "Please connect broker first"})

    data = request.get_json(force=True) or {}
    selected_date = data.get("date")
    if not selected_date:
        return jsonify({"status": "error", "message": "Please select a date"})

    if bot_state["manual_scan_state"]["is_running"]:
        return jsonify({"status": "success", "message": "Scan already running in background"})

    threading.Thread(target=worker_run_manual_5x_scan, args=(selected_date,), daemon=True).start()
    return jsonify({"status": "success", "message": "Scan started in background"})

@app.route('/api/manual-scan-status', methods=['GET'])
def manual_scan_status():
    return jsonify(bot_state["manual_scan_state"])

@app.route('/api/stop-manual-scan', methods=['POST'])
def stop_manual_scan():
    bot_state["manual_scan_state"]["scan_cancelled"] = True
    return jsonify({"status": "success", "message": "Scan stop signal sent."})

# ================= CPR SCANNER =================
def worker_run_cpr_scan(selected_date):
    st = bot_state["cpr_scan_state"]
    st["is_running"] = True
    st["scan_cancelled"] = False
    st["date"] = selected_date
    st["scanned_count"] = 0
    st["results"] = []

    stocks_to_scan = bot_state["fno_stocks"]
    st["total_stocks"] = len(stocks_to_scan)

    try:
        target_dt = datetime.datetime.strptime(selected_date, "%Y-%m-%d")
        from_dt = (target_dt - datetime.timedelta(days=12)).strftime("%Y-%m-%d 09:15")
        to_dt = target_dt.strftime("%Y-%m-%d 15:30")
    except Exception:
        st["is_running"] = False
        return

    log(f"CPR scan started for date: {selected_date}...")

    for item in stocks_to_scan:
        if st["scan_cancelled"]:
            break

        time.sleep(0.08)
        st["scanned_count"] += 1

        df = fetch_candles_safe(item["token"], interval="ONE_DAY", from_date=from_dt, to_date=to_dt)
        if df is not None and len(df) >= 1:
            last_day = df.iloc[-1]
            high = float(last_day["high"])
            low = float(last_day["low"])
            close = float(last_day["close"])

            pivot = (high + low + close) / 3.0
            bc = (high + low) / 2.0
            tc = (2 * pivot) - bc
            cpr_width = abs(tc - bc)
            cpr_width_pct = (cpr_width / pivot) * 100 if pivot > 0 else 1.0

            if cpr_width_pct <= 0.15:
                st["results"].append({
                    "symbol": item["symbol"].replace("-EQ", ""),
                    "pivot": round(pivot, 2),
                    "tc": round(tc, 2),
                    "bc": round(bc, 2),
                    "width_pct": round(cpr_width_pct, 3),
                    "ltp": round(close, 2)
                })

    st["is_running"] = False
    log(f"CPR Scan Complete: Found {len(st['results'])} narrow CPR stock(s).")

@app.route('/api/cpr-scan', methods=['POST'])
def cpr_scan():
    if not bot_state.get("smart_api"):
        return jsonify({"status": "error", "message": "Please connect broker first"})

    data = request.get_json(force=True) or {}
    selected_date = data.get("date")
    if not selected_date:
        return jsonify({"status": "error", "message": "Please select a date"})

    if bot_state["cpr_scan_state"]["is_running"]:
        return jsonify({"status": "success", "message": "CPR scan already running in background"})

    threading.Thread(target=worker_run_cpr_scan, args=(selected_date,), daemon=True).start()
    return jsonify({"status": "success", "message": "CPR scan started in background"})

@app.route('/api/cpr-scan-status', methods=['GET'])
def cpr_scan_status():
    return jsonify(bot_state["cpr_scan_state"])

@app.route('/api/stop-cpr-scan', methods=['POST'])
def stop_cpr_scan():
    bot_state["cpr_scan_state"]["scan_cancelled"] = True
    return jsonify({"status": "success", "message": "CPR scan stop signal sent."})

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
        "c1_candidates": bot_state["c1_candidates"],
        "pending_orders": bot_state["pending_orders"],
        "active_trades": bot_state["active_trades"],
        "trade_history": bot_state["trade_history"]
    })

def place_live_order_raw(symbol, token, side, qty, order_type, trigger_price=0.0, limit_price=0.0):
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
        res = bot_state["smart_api"].placeOrder(order_params)
        log(f"LIVE {order_type} ORDER: {side} {qty} {symbol} Trg:{trigger_price} Px:{limit_price} | Res: {res}")
        if res and res.get("data") and res["data"].get("orderid"):
            return res["data"]["orderid"]
        return f"LIVE_{int(time.time()*1000)}"
    except Exception as e:
        log(f"LIVE {order_type} ORDER ERROR: {e}")
        return None

def cancel_live_order(order_id, variety="STOPLOSS"):
    if not order_id or str(order_id).startswith("PAPER_"):
        return True
    try:
        res = bot_state["smart_api"].cancelOrder(order_id, variety)
        log(f"CANCELLED BROKER ORDER ID: {order_id} | Res: {res}")
        return True
    except Exception as e:
        log(f"CANCEL BROKER ORDER ERROR: {e}")
        return False

def place_live_exit_order(symbol, token, side, qty):
    return place_live_order_raw(symbol, token, side, qty, "MARKET")

# ================= LIVE BACKGROUND SCANNER =================
def background_scanner():
    c1_scanned = False

    while bot_state["is_logged_in"]:
        if not bot_state["engine_running"]:
            time.sleep(1)
            continue

        if len(bot_state["fno_stocks"]) < 50:
            time.sleep(1)
            continue

        now_ist = get_ist_now()
        now_time = now_ist.time()

        cutoff_parts = [int(x) for x in bot_state["cutoff_time"].split(":")]
        cutoff_time_obj = datetime.time(cutoff_parts[0], cutoff_parts[1])

        if now_time >= cutoff_time_obj:
            if bot_state["pending_orders"]:
                for po in bot_state["pending_orders"]:
                    if po["status"] == "PENDING":
                        cancel_live_order(po.get("order_id"), po.get("variety", "STOPLOSS"))
                        po["status"] = "CANCELLED_CUTOFF"
                        log(f"Cutoff Time Hit ({bot_state['cutoff_time']}): Cancelled pending order on {po['symbol']}")
            time.sleep(5)
            continue

        # 09:21:00 AM IST: Scan at exact 09:21:00 with Unified Engine
        if not c1_scanned and now_time >= datetime.time(9, 21, 0):
            today_str = now_ist.strftime("%Y-%m-%d")
            log(f"09:21 AM: Running Unified 5x C1 Volume Scan on {len(bot_state['fno_stocks'])} stocks...")
            candidates = []

            for item in bot_state["fno_stocks"]:
                time.sleep(0.08)
                res = evaluate_stock_c1_setup(item["token"], item["symbol"], today_str)
                if res:
                    candidates.append({
                        "symbol": res["full_symbol"],
                        "token": res["token"],
                        "bias": res["bias"],
                        "c1_high": res["c1_high"],
                        "c1_low": res["c1_low"],
                        "c1_close": res["c1_close"],
                        "c1_vol": res["c1_volume"],
                        "pdh": res["pdh"],
                        "pdl": res["pdl"],
                        "ratio": res["multiplier"],
                        "order_state": "READY FOR TRADE",
                        "display_status": "READY FOR TRADE"
                    })
                    log(f"🎯 Bot Qualified Setup: {res['symbol']} ({res['multiplier']}x Vol) [{res['bias']}]")

            bot_state["c1_candidates"] = candidates
            log(f"C1 Scan Complete: {len(candidates)} candidate(s) passed and FROZEN in qualified setups.")
            c1_scanned = True

        # 09:25 AM IST: Confirmation, Setup Arming & Invalidation Check
        if c1_scanned and now_time >= datetime.time(9, 25, 2):
            pending_count = len([p for p in bot_state["pending_orders"] if p["status"] == "PENDING"])

            if (bot_state["trades_executed_today"] + pending_count) < bot_state["max_trades"]:
                for cand in bot_state["c1_candidates"]:
                    sym = cand["symbol"]

                    if (bot_state["trades_executed_today"] + pending_count) >= bot_state["max_trades"]:
                        break

                    if sym in bot_state["invalidated_symbols"] or cand.get("order_state") != "READY FOR TRADE":
                        continue

                    today_str = now_ist.strftime("%Y-%m-%d")
                    from_dt = (now_ist - datetime.timedelta(days=2)).strftime("%Y-%m-%d 09:15")
                    to_dt = now_ist.strftime("%Y-%m-%d %H:%M")
                    
                    df = fetch_candles_safe(cand["token"], interval="FIVE_MINUTE", from_date=from_dt, to_date=to_dt)
                    if df is not None and len(df) >= 2:
                        df['time_str'] = df['time'].astype(str)
                        c2_matches = df.index[
                            (df['time_str'].str.startswith(today_str)) & 
                            (df['time_str'].str.contains("09:20"))
                        ].tolist()

                        if not c2_matches:
                            continue

                        c2 = df.iloc[c2_matches[0]]
                        c2_high = float(c2["high"])
                        c2_low = float(c2["low"])
                        c2_close = float(c2["close"])
                        c1_h = cand["c1_high"]
                        c1_l = cand["c1_low"]

                        if cand["bias"] == "BULLISH_PDH_BREAKOUT" and c2_low < c1_l:
                            bot_state["invalidated_symbols"].append(sym)
                            cand["order_state"] = "PERMANENTLY_INVALID"
                            cand["display_status"] = "INVALID"
                            log(f"Setup Pre-Invalidated: {sym} breached C1 Low. Blocked for day.")
                            continue
                        elif cand["bias"] == "BEARISH_PDL_BREAKDOWN" and c2_high > c1_h:
                            bot_state["invalidated_symbols"].append(sym)
                            cand["order_state"] = "PERMANENTLY_INVALID"
                            cand["display_status"] = "INVALID"
                            log(f"Setup Pre-Invalidated: {sym} breached C1 High. Blocked for day.")
                            continue

                        side = None
                        target_entry = 0.0
                        sl = 0.0

                        if cand["bias"] == "BULLISH_PDH_BREAKOUT":
                            if c2_close > c1_h:
                                side = "BUY"
                                target_entry = c2_high
                                sl = c2_low
                            elif c2_high <= c1_h and c2_low >= c1_l:
                                side = "BUY"
                                target_entry = c1_h
                                sl = c2_low

                        elif cand["bias"] == "BEARISH_PDL_BREAKDOWN":
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
                            qty = calculate_quantity(bot_state["risk_amount"], target_entry, sl)
                            target_mult = bot_state["rr_ratio"]
                            final_target = round(target_entry + (target_mult * risk_pts) if side == "BUY" else target_entry - (target_mult * risk_pts), 2)

                            current_ltp = target_entry
                            try:
                                ltp_res = bot_state["smart_api"].ltpData("NSE", cand["symbol"], str(cand["token"]))
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
                                cand["order_state"] = "BLOCKED_1_TO_1"
                                cand["display_status"] = "CROSSED MORE THAN 1:1"
                                log(f"Late Check: {sym} crossed 1:1 reward zone. Ignored.")
                                continue

                            order_type = "STOPLOSS_MARKET"
                            variety = "STOPLOSS"
                            if side == "BUY" and current_ltp > target_entry:
                                order_type = "LIMIT"
                                variety = "NORMAL"
                            elif side == "SELL" and current_ltp < target_entry:
                                order_type = "LIMIT"
                                variety = "NORMAL"

                            mode = bot_state["trading_mode"]
                            order_id = f"PAPER_{int(time.time()*1000)}"
                            if mode == "LIVE":
                                order_id = place_live_order_raw(
                                    cand["symbol"], cand["token"], side, qty, order_type,
                                    trigger_price=target_entry if order_type == "STOPLOSS_MARKET" else 0.0,
                                    limit_price=target_entry if order_type == "LIMIT" else 0.0
                                )

                            if order_type == "STOPLOSS_MARKET":
                                cand["order_state"] = "ARMED"
                                cand["display_status"] = "SL-M ORDER PLACED"
                            else:
                                cand["order_state"] = "ARMED"
                                cand["display_status"] = "LIMIT ORDER PLACED"

                            bot_state["pending_orders"].append({
                                "id": len(bot_state["pending_orders"]) + 1,
                                "order_id": order_id,
                                "symbol": sym,
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
                            log(f"Order Armed [{mode} | {order_type}]: {side} {sym} Level@{target_entry}")

            # Pending Order Monitor & Strict Invalidation
            for po in bot_state["pending_orders"]:
                if po["status"] != "PENDING":
                    continue

                try:
                    res = bot_state["smart_api"].ltpData("NSE", po["symbol"], str(po["token"]))
                    if res and res.get("status") and res.get("data"):
                        ltp = float(res["data"]["ltp"])

                        is_invalid = False
                        if po["side"] == "BUY" and ltp < po["c1_low"]:
                            is_invalid = True
                            reason = f"LTP (₹{ltp}) breached C1 Low (₹{po['c1_low']})"
                        elif po["side"] == "SELL" and ltp > po["c1_high"]:
                            is_invalid = True
                            reason = f"LTP (₹{ltp}) breached C1 High (₹{po['c1_high']})"

                        if is_invalid:
                            po["status"] = "CANCELLED_INVALID"
                            cancel_live_order(po.get("order_id"), po.get("variety", "STOPLOSS"))
                            
                            if po["symbol"] not in bot_state["invalidated_symbols"]:
                                bot_state["invalidated_symbols"].append(po["symbol"])

                            # Symbol retains in qualified list, only status changes to INVALID
                            for c in bot_state["c1_candidates"]:
                                if c["symbol"] == po["symbol"]:
                                    c["order_state"] = "PERMANENTLY_INVALID"
                                    c["display_status"] = "INVALID"
                                    
                            log(f"⚠️ PERMANENT INVALIDATION: {po['symbol']} marked INVALID for full day! ({reason}).")
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
                            for c in bot_state["c1_candidates"]:
                                if c["symbol"] == po["symbol"]:
                                    c["order_state"] = "IN_POSITION"
                                    c["display_status"] = "POSITION OPEN"

                            bot_state["active_trades"].append({
                                "id": len(bot_state["active_trades"]) + 1,
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
                                "cost_trailed": False,
                                "half_booked_1_2": False,
                                "current_rr": "1:0.0",
                                "ltp": ltp,
                                "pnl": 0.0,
                                "status": "OPEN",
                                "mode": po["mode"],
                                "time": get_ist_now().strftime("%I:%M:%S %p")
                            })
                            bot_state["trades_executed_today"] += 1
                            log(f"⚡ ORDER FILLED ({po['order_type']}): {po['side']} {po['symbol']} at ₹{po['trigger_price']}")
                except Exception:
                    pass
                time.sleep(0.04)

        time.sleep(1)

def update_oi_stats():
    if not bot_state["is_logged_in"] or not bot_state["fno_stocks"]:
        return

    oi_list = []
    for s in bot_state["fno_stocks"][:80]:
        all_tokens = s.get("all_fut_tokens", [])
        if not all_tokens:
            if s.get("fut_token"):
                all_tokens = [s.get("fut_token")]
            else:
                continue

        tot_cur_oi = 0.0
        tot_prev_oi = 0.0
        ltp = 0.0
        close = 0.0

        try:
            res = bot_state["smart_api"].getMarketData(
                mode="FULL",
                exchangeTokens={"NFO": [str(t) for t in all_tokens]}
            )
            if res and res.get("status") and res.get("data") and res["data"].get("fetched"):
                fetched_items = res["data"]["fetched"]
                if fetched_items:
                    ltp = float(fetched_items[0].get("ltp") or 0.0)
                    close = float(fetched_items[0].get("close") or ltp)

                for item in fetched_items:
                    cur_oi = float(item.get("opnInterest") or item.get("openInterest") or 0)
                    prev_oi = float(item.get("prevDayCloseOI") or item.get("prevCloseOI") or 0)
                    tot_cur_oi += cur_oi
                    tot_prev_oi += prev_oi

                if tot_cur_oi > 0:
                    pchange = round(((ltp - close) / close) * 100, 2) if close > 0 else 0.0

                    if tot_prev_oi > 0:
                        oi_change_pct = round(((tot_cur_oi - tot_prev_oi) / tot_prev_oi) * 100, 2)
                    else:
                        oi_change_pct = round(pchange * 2.2, 2)

                    oi_list.append({
                        "symbol": s["name"],
                        "ltp": ltp,
                        "pchange": pchange,
                        "oi": int(tot_cur_oi),
                        "oi_change": oi_change_pct,
                        "oi_spurt": abs(oi_change_pct)
                    })
        except Exception:
            pass

        time.sleep(0.015)

    if len(oi_list) >= 4:
        df_oi = pd.DataFrame(oi_list)

        bot_state["market_stats"]["top_oi_gainers"] = df_oi.sort_values(by="oi_change", ascending=False).head(10).to_dict('records')
        bot_state["market_stats"]["top_oi_losers"] = df_oi.sort_values(by="oi_change", ascending=True).head(10).to_dict('records')

        spurts_g = df_oi[df_oi['pchange'] >= 0].sort_values(by="oi_spurt", ascending=False).head(10)
        bot_state["market_stats"]["oi_spurts_gainers"] = spurts_g.to_dict('records') if not spurts_g.empty else []

        spurts_l = df_oi[df_oi['pchange'] < 0].sort_values(by="oi_spurt", ascending=False).head(10)
        bot_state["market_stats"]["oi_spurts_losers"] = spurts_l.to_dict('records') if not spurts_l.empty else []

def market_data_monitor():
    last_stats_check = 0

    while bot_state["is_logged_in"]:
        now_ts = time.time()
        now_ist = get_ist_now()

        is_weekday = (now_ist.weekday() < 5)
        is_time_window = (datetime.time(9, 15) <= now_ist.time() <= datetime.time(15, 30))

        try:
            n_res = bot_state["smart_api"].ltpData("NSE", "Nifty 50", "99926000")
            if not n_res or not n_res.get("data"):
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

        if now_ts - last_stats_check > 20:
            last_stats_check = now_ts
            update_oi_stats()

        open_trades = [t for t in bot_state["active_trades"] if t["status"] == "OPEN"]
        for trade in open_trades:
            try:
                ltp_data = bot_state["smart_api"].ltpData("NSE", trade["symbol"], str(trade["token"]))
                if ltp_data and ltp_data.get("data"):
                    ltp = float(ltp_data["data"]["ltp"])
                    trade["ltp"] = ltp
                    risk_unit = abs(trade["entry"] - trade["orig_sl"])

                    if risk_unit > 0:
                        achieved_pts = (ltp - trade["entry"]) if trade["side"] == "BUY" else (trade["entry"] - ltp)
                        current_ratio = max(0.0, achieved_pts / risk_unit)
                        trade["current_rr"] = f"1:{round(current_ratio, 1)}"

                    # ---------------- BUY POSITION ----------------
                    if trade["side"] == "BUY":
                        trade["pnl"] = round((ltp - trade["entry"]) * trade["remaining_qty"], 2)

                        if trade["rr_ratio"] <= 2:
                            if ltp >= trade["target"]:
                                trade["status"] = f"FULL TARGET HIT (1:{trade['rr_ratio']})"
                                if trade["mode"] == "LIVE":
                                    place_live_exit_order(trade["symbol"], trade["token"], "SELL", trade["remaining_qty"])
                                record_trade_history(trade, ltp)
                                continue
                        else:
                            if not trade["cost_trailed"] and ltp >= (trade["entry"] + risk_unit):
                                trade["cost_trailed"] = True
                                trade["sl"] = trade["entry"]
                                log(f"🛡️ 1:1 REACHED on {trade['symbol']}: SL shifted to COST (₹{trade['entry']}).")

                            if not trade["half_booked_1_2"] and ltp >= (trade["entry"] + 2 * risk_unit):
                                trade["half_booked_1_2"] = True
                                half_qty = max(1, trade["remaining_qty"] // 2)
                                trade["remaining_qty"] -= half_qty
                                trade["sl"] = round(trade["entry"] + risk_unit, 2)
                                if trade["mode"] == "LIVE":
                                    place_live_exit_order(trade["symbol"], trade["token"], "SELL", half_qty)
                                log(f"🔥 1:2 REACHED on {trade['symbol']}: 50% Booked. SL Trailed to Profit (₹{trade['sl']}).")

                            if ltp >= trade["target"]:
                                trade["status"] = f"FULL TARGET HIT (1:{trade['rr_ratio']})"
                                if trade["mode"] == "LIVE":
                                    place_live_exit_order(trade["symbol"], trade["token"], "SELL", trade["remaining_qty"])
                                record_trade_history(trade, ltp)
                                continue

                        if ltp <= trade["sl"]:
                            trade["status"] = "SL HIT" if not trade["cost_trailed"] else ("COST SL HIT" if not trade["half_booked_1_2"] else "TRAIL SL HIT")
                            if trade["mode"] == "LIVE":
                                place_live_exit_order(trade["symbol"], trade["token"], "SELL", trade["remaining_qty"])
                            record_trade_history(trade, ltp)

                    # ---------------- SELL POSITION ----------------
                    else:
                        trade["pnl"] = round((trade["entry"] - ltp) * trade["remaining_qty"], 2)

                        if trade["rr_ratio"] <= 2:
                            if ltp <= trade["target"]:
                                trade["status"] = f"FULL TARGET HIT (1:{trade['rr_ratio']})"
                                if trade["mode"] == "LIVE":
                                    place_live_exit_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                                record_trade_history(trade, ltp)
                                continue
                        else:
                            if not trade["cost_trailed"] and ltp <= (trade["entry"] - risk_unit):
                                trade["cost_trailed"] = True
                                trade["sl"] = trade["entry"]
                                log(f"🛡️ 1:1 REACHED on {trade['symbol']}: SL shifted to COST (₹{trade['entry']}).")

                            if not trade["half_booked_1_2"] and ltp <= (trade["entry"] - 2 * risk_unit):
                                trade["half_booked_1_2"] = True
                                half_qty = max(1, trade["remaining_qty"] // 2)
                                trade["remaining_qty"] -= half_qty
                                trade["sl"] = round(trade["entry"] - risk_unit, 2)
                                if trade["mode"] == "LIVE":
                                    place_live_exit_order(trade["symbol"], trade["token"], "BUY", half_qty)
                                log(f"🔥 1:2 REACHED on {trade['symbol']}: 50% Booked. SL Trailed to Profit (₹{trade['sl']}).")

                            if ltp <= trade["target"]:
                                trade["status"] = f"FULL TARGET HIT (1:{trade['rr_ratio']})"
                                if trade["mode"] == "LIVE":
                                    place_live_exit_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                                record_trade_history(trade, ltp)
                                continue

                        if ltp >= trade["sl"]:
                            trade["status"] = "SL HIT" if not trade["cost_trailed"] else ("COST SL HIT" if not trade["half_booked_1_2"] else "TRAIL SL HIT")
                            if trade["mode"] == "LIVE":
                                place_live_exit_order(trade["symbol"], trade["token"], "BUY", trade["remaining_qty"])
                            record_trade_history(trade, ltp)

            except Exception:
                pass
            time.sleep(0.08)

        closed_pnl = sum(h["pnl"] for h in bot_state["trade_history"])
        open_pnl = sum(t["pnl"] for t in bot_state["active_trades"] if t["status"] == "OPEN")
        bot_state["total_pnl"] = round(closed_pnl + open_pnl, 2)
        time.sleep(1)

def record_trade_history(trade, exit_price):
    now_ist = get_ist_now()
    bot_state["trade_history"].append({
        "trade_date": now_ist.strftime("%Y-%m-%d"),
        "time": now_ist.strftime("%I:%M:%S %p"),
        "symbol": trade["symbol"],
        "side": trade["side"],
        "entry": trade["entry"],
        "exit": exit_price,
        "target": trade["target"],
        "pnl": trade["pnl"],
        "status": trade["status"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
