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
    "active_trades": [],
    "trade_history": [],
    "status_log": "Terminal Ready. Please connect broker."
}

def log(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    bot_state["status_log"] = f"[{timestamp}] {msg}"
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
        log(f"Loaded {len(bot_state['fno_stocks'])} Stocks.")
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
            log("Broker Connected! Live data stream active.")

            load_fno_symbols()
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
    log(f"Engine status changed: {status_str}")
    return jsonify({"status": "success", "engine_running": bot_state["engine_running"]})

@app.route('/api/set-mode', methods=['POST'])
def set_mode():
    data = request.json
    mode = data.get("mode", "PAPER").upper()
    if mode in ["PAPER", "LIVE"]:
        bot_state["trading_mode"] = mode
        log(f"Mode changed to: {mode}")
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
        "active_trades": bot_state["active_trades"],
        "trade_history": bot_state["trade_history"]
    })

def market_data_monitor():
    """Live NIFTY 50 & SENSEX price + % change calculation"""
    while bot_state["is_logged_in"]:
        try:
            # Nifty 50 Spot (Token 99926000)
            n_res = bot_state["smart_api"].ltpData("NSE", "NIFTY", "99926000")
            if n_res and n_res.get("data"):
                ltp = float(n_res["data"]["ltp"])
                close = float(n_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["NIFTY"] = {"ltp": ltp, "change": change, "pchange": pchange}

            # Sensex Spot (Token 99919000 on BSE)
            s_res = bot_state["smart_api"].ltpData("BSE", "SENSEX", "99919000")
            if s_res and s_res.get("data"):
                ltp = float(s_res["data"]["ltp"])
                close = float(s_res["data"].get("close", ltp))
                change = round(ltp - close, 2)
                pchange = round((change / close) * 100, 2) if close > 0 else 0.0
                bot_state["market_indices"]["SENSEX"] = {"ltp": ltp, "change": change, "pchange": pchange}
        except Exception:
            pass

        # Real-time open trades P&L tracking
        open_trades = [t for t in bot_state["active_trades"] if t["status"] == "OPEN"]
        for trade in open_trades:
            try:
                ltp_data = bot_state["smart_api"].ltpData("NSE", trade["symbol"], str(trade["token"]))
                if ltp_data and ltp_data.get("data"):
                    ltp = float(ltp_data["data"]["ltp"])
                    trade["ltp"] = ltp
                    if trade["side"] == "BUY":
                        trade["pnl"] = round((ltp - trade["entry"]) * trade["qty"], 2)
                    else:
                        trade["pnl"] = round((trade["entry"] - ltp) * trade["qty"], 2)
            except Exception:
                pass
            time.sleep(0.1)

        bot_state["total_pnl"] = round(sum(t["pnl"] for t in bot_state["active_trades"]), 2)
        time.sleep(2)

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
            # Top Gainers & Losers
            gainers = df_perf.sort_values(by="pchange", ascending=False).head(5).to_dict('records')
            losers = df_perf.sort_values(by="pchange", ascending=True).head(5).to_dict('records')
            # Volume Buzzers
            volume_top = df_perf.sort_values(by="volume", ascending=False).head(5).to_dict('records')

            bot_state["market_stats"]["top_gainers"] = gainers
            bot_state["market_stats"]["top_losers"] = losers
            bot_state["market_stats"]["volume_buzzers"] = volume_top

        time.sleep(25)  # Har 25 second me stats refresh

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
