import os
import time
import pyotp
import requests
import datetime
import threading
import pandas as pd
from flask import Flask, render_template, request, jsonify
from SmartApi import SmartConnect

app = Flask(__name__)

bot_state = {
    "is_logged_in": False,
    "engine_running": True,   # Start / Stop toggle state
    "smart_api": None,
    "feed_token": None,
    "risk_amount": 500,
    "trading_mode": "PAPER",  # 'PAPER' ya 'LIVE'
    "fno_stocks": [],
    "c1_candidates": [],
    "active_trades": [],
    "total_pnl": 0.0,
    "market_indices": {"NIFTY": 0.0, "SENSEX": 0.0},
    "status_log": "System ready. Please login to start."
}

def log(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    bot_state["status_log"] = f"[{timestamp}] {msg}"
    print(bot_state["status_log"])

def load_fno_cash_symbols():
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

        bot_state["fno_stocks"] = stocks[:160]
        log(f"Loaded {len(bot_state['fno_stocks'])} Nifty F&O Cash Stocks.")
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
            log("Angel One connected! Live threads started.")

            threading.Thread(target=background_scanner, daemon=True).start()
            threading.Thread(target=live_pnl_and_indices_tracker, daemon=True).start()

            return jsonify({"status": "success", "message": "Connected to Angel One!"})
        else:
            return jsonify({"status": "error", "message": login_res.get("message", "Login failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/toggle-engine', methods=['POST'])
def toggle_engine():
    data = request.json
    bot_state["engine_running"] = data.get("running", True)
    state_str = "STARTED" if bot_state["engine_running"] else "STOPPED (PAUSED)"
    log(f"Algo Engine manual status: {state_str}")
    return jsonify({"status": "success", "engine_running": bot_state["engine_running"]})

@app.route('/api/set-risk', methods=['POST'])
def set_risk():
    data = request.json
    risk = int(data.get("risk", 500))
    if 50 <= risk <= 5000:
        bot_state["risk_amount"] = risk
        return jsonify({"status": "success", "risk": risk})
    return jsonify({"status": "error", "message": "Risk limit error"})

@app.route('/api/set-mode', methods=['POST'])
def set_mode():
    data = request.json
    mode = data.get("mode", "PAPER").upper()
    if mode in ["PAPER", "LIVE"]:
        bot_state["trading_mode"] = mode
        log(f"Switched Trading Mode to: {mode}")
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error", "message": "Invalid mode"})

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify({
        "logged_in": bot_state["is_logged_in"],
        "engine_running": bot_state["engine_running"],
        "status": bot_state["status_log"],
        "risk_amount": bot_state["risk_amount"],
        "trading_mode": bot_state["trading_mode"],
        "total_pnl": bot_state["total_pnl"],
        "market_indices": bot_state["market_indices"],
        "c1_candidates": bot_state["c1_candidates"],
        "active_trades": bot_state["active_trades"]
    })

@app.route('/api/offline-scan', methods=['POST'])
def offline_scan():
    """Kisi bhi chuni gayi date ka 5x volume C1 breakout scan karna"""
    if not bot_state["is_logged_in"]:
        return jsonify({"status": "error", "message": "Pehle broker login karein."})
    
    data = request.json
    target_date = data.get("date") # Format: YYYY-MM-DD
    if not target_date:
        return jsonify({"status": "error", "message": "Date select karein."})
    
    try:
        t_date = datetime.datetime.strptime(target_date, "%Y-%m-%d")
        from_date = (t_date - datetime.timedelta(days=5)).strftime("%Y-%m-%d 09:15")
        to_date = t_date.strftime("%Y-%m-%d 15:30")
    except Exception:
        return jsonify({"status": "error", "message": "Invalid Date format."})

    results = []
    # Fast scan top 30 FnO stocks for on-demand offline query
    scan_list = bot_state["fno_stocks"][:35] if bot_state["fno_stocks"] else []
    
    for item in scan_list:
        time.sleep(0.3)
        params = {
            "exchange": "NSE",
            "symboltoken": str(item["token"]),
            "interval": "FIVE_MINUTE",
            "fromdate": from_date,
            "todate": to_date
        }
        try:
            cand = bot_state["smart_api"].getCandleData(params)
            if cand and cand.get("status") and cand.get("data"):
                df = pd.DataFrame(cand["data"], columns=["time", "open", "high", "low", "close", "volume"])
                
                # Check 9:15 candle for that target date
                c1_matches = df[df['time'].str.startswith(f"{target_date}T09:15")]
                if not c1_matches.empty:
                    idx = c1_matches.index[0]
                    if idx >= 20:
                        prev_avg = df.iloc[idx-20:idx]["volume"].mean()
                        c1_vol = float(c1_matches.iloc[0]["volume"])
                        if prev_avg > 0 and (c1_vol >= 5 * prev_avg):
                            results.append({
                                "symbol": item["symbol"],
                                "ratio": round(c1_vol / prev_avg, 2),
                                "c1_high": float(c1_matches.iloc[0]["high"]),
                                "c1_low": float(c1_matches.iloc[0]["low"]),
                                "c1_vol": int(c1_vol),
                                "avg_vol": int(prev_avg)
                            })
        except Exception:
            pass

    return jsonify({"status": "success", "date": target_date, "data": results})

def calculate_quantity(risk, entry, sl):
    risk_per_share = abs(entry - sl)
    return 1 if risk_per_share <= 0 else max(1, int(risk / risk_per_share))

def fetch_candles(token, interval="FIVE_MINUTE", days=2):
    now = datetime.datetime.now()
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
        log(f"LIVE ORDER FAILED: {e}")
        return None

def background_scanner():
    load_fno_cash_symbols()
    c1_scanned = False
    c2_scanned = False

    while bot_state["is_logged_in"]:
        if not bot_state["engine_running"]:
            time.sleep(1)
            continue

        now_time = datetime.datetime.now().time()

        # Step 1: 9:20 AM C1 Volume Check
        if not c1_scanned and now_time >= datetime.time(9, 20, 2):
            log("9:20 AM: Scanning C1 5x Volume Breakouts...")
            qualified = []

            for item in bot_state["fno_stocks"]:
                time.sleep(0.35)
                df = fetch_candles(item["token"])
                if df is not None and len(df) >= 22:
                    prev_20_vol_avg = df.iloc[-22:-2]["volume"].mean()
                    c1_candle = df.iloc[-2]
                    c1_vol = c1_candle["volume"]

                    if prev_20_vol_avg > 0 and (c1_vol >= 5 * prev_20_vol_avg):
                        ratio = round(c1_vol / prev_20_vol_avg, 2)
                        qualified.append({
                            "symbol": item["symbol"],
                            "token": item["token"],
                            "c1_high": float(c1_candle["high"]),
                            "c1_low": float(c1_candle["low"]),
                            "c1_vol": int(c1_vol),
                            "avg_vol": int(prev_20_vol_avg),
                            "ratio": ratio
                        })
            bot_state["c1_candidates"] = qualified
            log(f"C1 Scan Complete: {len(qualified)} stocks matched.")
            c1_scanned = True

        # Step 2: 9:25 AM C2 Breakout Check & Execution
        if c1_scanned and not c2_scanned and now_time >= datetime.time(9, 25, 2):
            log("9:25 AM: Checking C2 Breakout Confirmation...")

            for cand in bot_state["c1_candidates"]:
                time.sleep(0.35)
                df = fetch_candles(cand["token"])
                if df is not None and len(df) >= 2:
                    c2_candle = df.iloc[-1]
                    c2_close = float(c2_candle["close"])
                    c2_high = float(c2_candle["high"])
                    c2_low = float(c2_candle["low"])

                    side = None
                    entry = c2_close
                    sl = 0

                    if c2_close > cand["c1_high"]:
                        side = "BUY"
                        sl = c2_low
                    elif c2_close < cand["c1_low"]:
                        side = "SELL"
                        sl = c2_high

                    if side:
                        risk_pts = abs(entry - sl)
                        qty = calculate_quantity(bot_state["risk_amount"], entry, sl)
                        target = round(entry + (2 * risk_pts) if side == "BUY" else entry - (2 * risk_pts), 2)
                        mode = bot_state["trading_mode"]

                        status = "OPEN"
                        if mode == "LIVE":
                            live_res = place_live_order(cand["symbol"], cand["token"], side, qty)
                            if not live_res:
                                status = "FAILED"

                        bot_state["active_trades"].append({
                            "id": len(bot_state["active_trades"]) + 1,
                            "symbol": cand["symbol"],
                            "token": cand["token"],
                            "side": side,
                            "entry": entry,
                            "sl": sl,
                            "target": target,
                            "qty": qty,
                            "ltp": entry,
                            "pnl": 0.0,
                            "status": status,
                            "mode": mode,
                            "time": datetime.datetime.now().strftime("%H:%M:%S")
                        })
            c2_scanned = True
            log("9:25 AM Orders finished.")

        time.sleep(1)

def live_pnl_and_indices_tracker():
    """Live Nifty / Sensex Fetcher + Trade P&L Monitor"""
    while bot_state["is_logged_in"]:
        try:
            # 1. Fetch Real-time Spot: NIFTY 50 (Token 99926000) & SENSEX (Token 99919000 on BSE)
            nifty_res = bot_state["smart_api"].ltpData("NSE", "NIFTY", "99926000")
            if nifty_res and nifty_res.get("data"):
                bot_state["market_indices"]["NIFTY"] = float(nifty_res["data"]["ltp"])

            sensex_res = bot_state["smart_api"].ltpData("BSE", "SENSEX", "99919000")
            if sensex_res and sensex_res.get("data"):
                bot_state["market_indices"]["SENSEX"] = float(sensex_res["data"]["ltp"])
        except Exception:
            pass

        # 2. Monitor Open Positions
        open_trades = [t for t in bot_state["active_trades"] if t["status"] == "OPEN"]
        for trade in open_trades:
            try:
                ltp_data = bot_state["smart_api"].ltpData("NSE", trade["symbol"], str(trade["token"]))
                if ltp_data and ltp_data.get("data"):
                    ltp = float(ltp_data["data"]["ltp"])
                    trade["ltp"] = ltp

                    if trade["side"] == "BUY":
                        trade["pnl"] = round((ltp - trade["entry"]) * trade["qty"], 2)
                        if ltp <= trade["sl"]:
                            trade["status"] = "SL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "SELL", trade["qty"])
                        elif ltp >= trade["target"]:
                            trade["status"] = "TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "SELL", trade["qty"])
                    else:
                        trade["pnl"] = round((trade["entry"] - ltp) * trade["qty"], 2)
                        if ltp >= trade["sl"]:
                            trade["status"] = "SL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "BUY", trade["qty"])
                        elif ltp <= trade["target"]:
                            trade["status"] = "TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "BUY", trade["qty"])
            except Exception:
                pass
            time.sleep(0.2)

        bot_state["total_pnl"] = round(sum(t["pnl"] for t in bot_state["active_trades"]), 2)
        time.sleep(2)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
