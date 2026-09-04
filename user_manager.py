import os
import json
import pyotp
from SmartApi import SmartConnect

USER_DIR = "user_configs"
if not os.path.exists(USER_DIR):
    os.makedirs(USER_DIR)

active_user_sessions = {}

def get_user_config_path(client_code):
    return os.path.join(USER_DIR, f"config_{client_code}.json")

def load_user_settings(client_code):
    default_config = {
        "engine_running": True,
        "max_trades": 3,
        "rr_ratio": 3,
        "cutoff_time": "15:00",
        "risk_amount": 500,
        "trading_mode": "PAPER"
    }
    path = get_user_config_path(client_code)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return {**default_config, **json.load(f)}
        except Exception:
            pass
    return default_config

def save_user_settings(client_code, data):
    path = get_user_config_path(client_code)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving config for {client_code}: {e}")

def authenticate_and_create_session(api_key, client_code, pin, totp_secret):
    client_code = client_code.strip().upper()
    try:
        smart_api = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        login_res = smart_api.generateSession(client_code, pin, totp)

        if login_res.get('status'):
            feed_token = smart_api.getfeedToken()
            saved_conf = load_user_settings(client_code)

            active_user_sessions[client_code] = {
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
                "status_log": "Connected. Strategy Active."
            }
            return True, client_code, "Login Successful"
        else:
            return False, None, login_res.get("message", "Login Failed")
    except Exception as e:
        return False, None, str(e)

def get_user(client_code):
    return active_user_sessions.get(client_code)
