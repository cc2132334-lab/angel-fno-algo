import os
import sys
import time
import sqlite3
import datetime
import subprocess
import requests
from flask import Flask, request, Response, redirect, render_template, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("GATEWAY_SECRET", "super_secure_master_key_9988")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin@123")
DB_FILE = "users.db"

# Running child bot processes: { "username": {"port": 5001, "proc": PopenObject} }
running_instances = {}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            port INTEGER,
            status TEXT DEFAULT 'ACTIVE',
            expiry_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Column check for existing tables
    c.execute("PRAGMA table_info(users)")
    cols = [info[1] for info in c.fetchall()]
    if "expiry_date" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN expiry_date TEXT")
    conn.commit()
    conn.close()

init_db()

def get_next_port():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT MAX(port) FROM users")
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return row[0] + 1
    return 5001

def is_expired(expiry_str):
    if not expiry_str:
        return False
    try:
        exp_dt = datetime.datetime.strptime(expiry_str, "%Y-%m-%d").date()
        today = datetime.date.today()
        return today > exp_dt
    except Exception:
        return False

def ensure_user_process(username, port):
    if username in running_instances:
        proc = running_instances[username]["proc"]
        if proc.poll() is None:
            return True

    env = os.environ.copy()
    env["PORT"] = str(port)
    env["CONFIG_FILE"] = f"bot_config_{username}.json"

    proc = subprocess.Popen([sys.executable, "server.py"], env=env)
    running_instances[username] = {"port": port, "proc": proc}
    
    for _ in range(25):
        time.sleep(0.3)
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/state", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
    return True

# ================= AUTH & LOGIN =================

@app.route('/portal-login', methods=['GET', 'POST'])
def portal_login():
    if request.method == 'GET':
        return render_template('login.html', view="login")
    
    data = request.form or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if username == ADMIN_USER and password == ADMIN_PASS:
        session["is_admin"] = True
        return redirect('/admin')

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, password, port, status, expiry_date FROM users WHERE username = ?", (username,))
    row = c.fetchone()

    if not row:
        conn.close()
        return render_template('login.html', view="login", error="User not registered. Contact admin.")

    uid, db_pass, port, status, expiry_date = row

    if db_pass != password:
        conn.close()
        return render_template('login.html', view="login", error="Invalid Password.")

    # Expiry Check
    if is_expired(expiry_date):
        c.execute("UPDATE users SET status = 'BLOCKED' WHERE id = ?", (uid,))
        conn.commit()
        conn.close()
        if username in running_instances:
            try:
                running_instances[username]["proc"].terminate()
            except Exception:
                pass
            running_instances.pop(username, None)
        return render_template('login.html', view="login", error=f"Subscription Expired on {expiry_date}. Contact admin to renew.")

    if status != 'ACTIVE':
        conn.close()
        return render_template('login.html', view="login", error="Account Blocked. Contact admin.")

    conn.close()
    session["user"] = username
    session["port"] = port
    ensure_user_process(username, port)
    return redirect('/')

@app.route('/portal-logout')
def portal_logout():
    session.clear()
    return redirect('/portal-login')

# ================= ADMIN BACKOFFICE =================

@app.route('/admin')
def admin_panel():
    if not session.get("is_admin"):
        return redirect('/portal-login')

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, username, password, port, status, expiry_date, created_at FROM users ORDER BY id DESC")
    users = c.fetchall()
    conn.close()

    user_list = []
    today = datetime.date.today()
    for u in users:
        is_live = False
        if u[1] in running_instances and running_instances[u[1]]["proc"].poll() is None:
            is_live = True

        exp_str = u[5] or "Unlimited"
        days_left = "--"
        expired = False
        if u[5]:
            try:
                exp_dt = datetime.datetime.strptime(u[5], "%Y-%m-%d").date()
                diff = (exp_dt - today).days
                if diff < 0:
                    days_left = "Expired"
                    expired = True
                else:
                    days_left = f"{diff} days left"
            except Exception:
                pass

        user_list.append({
            "id": u[0], "username": u[1], "password": u[2], "port": u[3],
            "status": u[4], "expiry_date": exp_str, "days_left": days_left,
            "expired": expired, "created_at": u[6], "is_live": is_live
        })

    # Default date in add-user form: 30 days from today
    default_exp = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    return render_template('login.html', view="admin", users=user_list, default_exp=default_exp)

@app.route('/admin/add-user', methods=['POST'])
def admin_add_user():
    if not session.get("is_admin"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    expiry_date = request.form.get("expiry_date", "").strip()

    if not username or not password:
        return redirect('/admin')

    port = get_next_port()
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password, port, status, expiry_date) VALUES (?, ?, ?, 'ACTIVE', ?)", 
                  (username, password, port, expiry_date or None))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return redirect('/admin')

@app.route('/admin/update-expiry/<int:user_id>', methods=['POST'])
def admin_update_expiry(user_id):
    if not session.get("is_admin"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    new_expiry = request.form.get("new_expiry", "").strip()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET expiry_date = ?, status = 'ACTIVE' WHERE id = ?", (new_expiry or None, user_id))
    conn.commit()
    conn.close()
    return redirect('/admin')

@app.route('/admin/toggle-status/<int:user_id>')
def admin_toggle_status(user_id):
    if not session.get("is_admin"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, status FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row:
        uname, cur_status = row
        new_status = 'BLOCKED' if cur_status == 'ACTIVE' else 'ACTIVE'
        c.execute("UPDATE users SET status = ? WHERE id = ?", (new_status, user_id))
        conn.commit()

        if new_status == 'BLOCKED' and uname in running_instances:
            try:
                running_instances[uname]["proc"].terminate()
            except Exception:
                pass
            running_instances.pop(uname, None)

    conn.close()
    return redirect('/admin')

# ================= MASTER PROXY ROUTER =================

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def master_proxy_handler(path):
    if path.startswith('portal-') or path.startswith('admin'):
        return Response("Not found", status=404)

    user = session.get("user")
    port = session.get("port")

    if not user or not port:
        return redirect('/portal-login')

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, status, expiry_date FROM users WHERE username = ?", (user,))
    row = c.fetchone()

    if not row:
        conn.close()
        session.clear()
        return redirect('/portal-login')

    uid, status, expiry_date = row
    
    # Active runtime expiry validation
    if is_expired(expiry_date):
        c.execute("UPDATE users SET status = 'BLOCKED' WHERE id = ?", (uid,))
        conn.commit()
        conn.close()
        if user in running_instances:
            try:
                running_instances[user]["proc"].terminate()
            except Exception:
                pass
            running_instances.pop(user, None)
        session.clear()
        return redirect('/portal-login')

    if status != 'ACTIVE':
        conn.close()
        session.clear()
        return redirect('/portal-login')

    conn.close()
    ensure_user_process(user, port)

    target_url = f"http://127.0.0.1:{port}/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != 'host'}

    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30
        )
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        resp_headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded_headers]
        return Response(resp.content, resp.status_code, resp_headers)
    except Exception:
        return f"<h3>Initializing terminal for {user}... Please refresh in 5 seconds.</h3>", 503

if __name__ == '__main__':
    main_port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=main_port)
