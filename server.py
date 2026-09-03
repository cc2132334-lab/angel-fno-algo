import os
import time
import pyotp
import requests
import datetime
import threading
import pandas as pd
from dateutil import tz
from flask import Flask, render_template, request, jsonify
from SmartApi import SmartConnect

app = Flask(__name__)

# Indian Standard Time (IST) Timezone Definition
IST = tz.gettz('Asia/Kolkata')

def get_ist_now():
    """Hamesha Indian Standard Time (IST) return karega"""
    return datetime.datetime.now(IST)

bot_state = {
    "is_logged_in": False,
    "engine_running": True,
    "smart_api": None,
    "feed_token": None,
    "risk_amount": 500,
    "trading_mode": "PAPER",
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
    "status_log": "Terminal Ready (IST Mode). Please login to start."
}

def log(msg):
    timestamp = get_ist_now().strftime("%H:%M:%S")
    bot_state["status_log"] = f"[{timestamp} IST] {msg}"
    print(bot_state["status_log"])

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
            log("Broker Connected! Live streams active.")

            load_fno_symbols()
            threading.Thread(target=background_scanner, daemon=True).start()
            threading.Thread(target=market_data_monitor, daemon=True).start()
            threading.Thread(target=market_stats_scanner, daemon=True).start()

            return jsonify({"status": "success", "message": "Login Successful!"})
        else:
            return jsonify({"status": "error", "message": login_res.get("message", "Login failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/toggle-engine', methods=['POST'])
def toggle_engine():
    data = request.json
    bot_state["engine_running"] = data.get("running", True)
    status_str = "RUNNING" if bot_state["engine_running"] else "STOPPED"
    log(f"Engine status: {status_str}")
    return jsonify({"status": "success", "engine_running": bot_state["engine_running"]})

@app.route('/api/set-risk', methods=['POST'])
def set_risk():
    data = request.json
    risk = int(data.get("risk", 500))
    if 50 <= risk <= 5000:
        bot_state["risk_amount"] = risk
        return jsonify({"status": "success", "risk": risk})
    return jsonify({"status": "error", "message": "Invalid risk limit"})

@app.route('/api/set-mode', methods=['POST'])
def set_mode():
    data = request.json
    mode = data.get("mode", "PAPER").upper()
    if mode in ["PAPER", "LIVE"]:
        bot_state["trading_mode"] = mode
        log(f"Trading Mode switched to: {mode}")
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error", "message": "Invalid mode"})

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify({
        "logged_in": bot_state["is_logged_in"],
        "engine_running": bot_state["engine_running"],
        "trading_mode": bot_state["trading_mode"],
        "status": bot_state["status_log"],
        "total_pnl": bot_state["total_pnl"],
        "market_indices": bot_state["market_indices"],
        "market_stats": bot_state["market_stats"],
        "c1_candidates": bot_state["c1_candidates"],
        "active_trades": bot_state["active_trades"],
        "trade_history": bot_state["trade_history"]
    })

def calculate_quantity(risk, entry, sl):
    risk_per_share = abs(entry - sl)
    return 1 if risk_per_share <= 0 else max(1, int(risk / risk_per_share))

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
        log(f"LIVE ORDER: {side} {qty} {symbol} | Res: {res}")
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

        now_time = get_ist_now().time()

        # Step 1: 09:20 AM IST - C1 Volume Check (5x of last 20 avg)
        if not c1_scanned and now_time >= datetime.time(9, 20, 2):
            log("Running 9:20 AM IST C1 Volume Scanner...")
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
            log(f"C1 Scan Done. {len(qualified)} stocks qualified.")
            c1_scanned = True

        # Step 2: 09:25 AM IST - C2 Breakout Check & Order Execution
        if c1_scanned and not c2_scanned and now_time >= datetime.time(9, 25, 2):
            log("Running 9:25 AM IST C2 Breakout Confirmation...")

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
                            "time": get_ist_now().strftime("%H:%M:%S")
                        })
            c2_scanned = True
            log("9:25 AM IST Breakout orders completed.")

        time.sleep(1)

def market_data_monitor():
    """Live NIFTY 50 & SENSEX + Live Open Trades P&L Monitor"""
    while bot_state["is_logged_in"]:
        try:
            # Nifty 50 Spot
            n_res = bot_state["smart_api"].ltpData("NSE", "NIFTY", "99926000")
            if n_res and n_res.get("data"):
                ltp = float(n_res["data"]["ltp"])
                close = float(n_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["NIFTY"] = {"ltp": ltp, "change": change, "pchange": pchange}

            # Sensex Spot
            s_res = bot_state["smart_api"].ltpData("BSE", "SENSEX", "99919000")
            if s_res and s_res.get("data"):
                ltp = float(s_res["data"]["ltp"])
                close = float(s_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["SENSEX"] = {"ltp": ltp, "change": change, "pchange": pchange}
        except Exception:
            pass

        # Real-time Position Monitoring (SL / Target / Trailing)
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
                            record_trade_history(trade, ltp)
                        elif ltp >= trade["target"]:
                            trade["status"] = "TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "SELL", trade["qty"])
                            record_trade_history(trade, ltp)
                    else:
                        trade["pnl"] = round((trade["entry"] - ltp) * trade["qty"], 2)
                        if ltp >= trade["sl"]:
                            trade["status"] = "SL HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "BUY", trade["qty"])
                            record_trade_history(trade, ltp)
                        elif ltp <= trade["target"]:
                            trade["status"] = "TARGET HIT"
                            if trade["mode"] == "LIVE":
                                place_live_order(trade["symbol"], trade["token"], "BUY", trade["qty"])
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
    """Top Gainers, Top Losers aur Volume Gainers Scanner"""
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
