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
    "smart_api": None,
    "feed_token": None,
    "risk_amount": 500,
    "trading_mode": "PAPER",  # 'PAPER' ya 'LIVE'
    "fno_stocks": [],
    "c1_candidates": [],
    "active_trades": [],
    "total_pnl": 0.0,
    "status_log": "System ready. Please login with Angel One credentials."
}

def log(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    bot_state["status_log"] = f"[{timestamp}] {msg}"
    print(bot_state["status_log"])

def load_fno_cash_symbols():
    """Angel One Scrip Master se Nifty FnO cash stock list aur token load karna"""
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = requests.get(url, timeout=12)
        master = res.json()

        nse_equities = {
            s['symbol'].replace('-EQ', ''): s['token'] 
            for s in master 
            if s.get('exch_seg') == 'NSE' and str(s.get('symbol')).endswith('-EQ')
        }

        # NSE Official FnO Lots CSV
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
        log(f"Successfully loaded {len(bot_state['fno_stocks'])} Nifty F&O Cash Stocks.")
    except Exception as e:
        log(f"Error loading scrip master: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    api_key = data.get("api_key")
    client_code = data.get("client_code")
    pin = data.get("pin")
    totp_secret = data.get("totp_secret")

    try:
        smart_api = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        login_res = smart_api.generateSession(client_code, pin, totp)

        if login_res.get('status'):
            bot_state["smart_api"] = smart_api
            bot_state["feed_token"] = smart_api.getfeedToken()
            bot_state["is_logged_in"] = True
            log("Angel One connected! Starting background threads...")

            threading.Thread(target=background_scanner, daemon=True).start()
            threading.Thread(target=live_pnl_tracker, daemon=True).start()

            return jsonify({"status": "success", "message": "Connected to Angel One!"})
        else:
            return jsonify({"status": "error", "message": login_res.get("message", "Login failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/set-risk', methods=['POST'])
def set_risk():
    data = request.json
    risk = int(data.get("risk", 500))
    if 50 <= risk <= 5000:
        bot_state["risk_amount"] = risk
        return jsonify({"status": "success", "risk": risk})
    return jsonify({"status": "error", "message": "Risk must be between ₹50 and ₹5000"})

@app.route('/api/set-mode', methods=['POST'])
def set_mode():
    data = request.json
    selected_mode = data.get("mode", "PAPER").upper()
    if selected_mode in ["PAPER", "LIVE"]:
        bot_state["trading_mode"] = selected_mode
        log(f"Switched Trading Mode to: {selected_mode}")
        return jsonify({"status": "success", "mode": selected_mode})
    return jsonify({"status": "error", "message": "Invalid mode"})

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify({
        "logged_in": bot_state["is_logged_in"],
        "status": bot_state["status_log"],
        "risk_amount": bot_state["risk_amount"],
        "trading_mode": bot_state["trading_mode"],
        "total_pnl": bot_state["total_pnl"],
        "c1_candidates": bot_state["c1_candidates"],
        "active_trades": bot_state["active_trades"]
    })

def calculate_quantity(risk, entry, sl):
    risk_per_share = abs(entry - sl)
    if risk_per_share <= 0:
        return 1
    return max(1, int(risk / risk_per_share))

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
        now_time = datetime.datetime.now().time()

        # Step 1: 9:20 AM C1 Volume Check (5x of 20-Avg)
        if not c1_scanned and now_time >= datetime.time(9, 20, 2):
            log("Running 9:20 AM C1 Volume Scanner...")
            qualified = []

            for item in bot_state["fno_stocks"]:
                time.sleep(0.35)  # Rate limiting protection
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
                        log(f"Qualified: {item['symbol']} ({ratio}x volume)")

            bot_state["c1_candidates"] = qualified
            log(f"C1 Scan Finished. Total: {len(qualified)} stocks qualified.")
            c1_scanned = True

        # Step 2: 9:25 AM C2 Breakout Check & Order Fire
        if c1_scanned and not c2_scanned and now_time >= datetime.time(9, 25, 2):
            log("Running 9:25 AM C2 Breakout Confirmation...")

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
                        log(f"Trade Executed [{mode}]: {side} {qty} {cand['symbol']} @ ₹{entry}")

            c2_scanned = True
            log("C2 Breakout checks completed.")

        time.sleep(1)

def live_pnl_tracker():
    """Live tick monitoring for open trades (SL & Target handling)"""
    while bot_state["is_logged_in"]:
        open_trades = [t for t in bot_state["active_trades"] if t["status"] == "OPEN"]

        for trade in open_trades:
            try:
                ltp_data = bot_state["smart_api"].ltpData("NSE", trade["symbol"], str(trade["token"]))
                if ltp_data and ltp_data.get("status") and ltp_data.get("data"):
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
