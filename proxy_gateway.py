import os
import sys
import time
import sqlite3
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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

def ensure_user_process(username, port):
    # Agar user process pehle se run nahi ho raha toh server.py ko naye port par start karega
    if username in running_instances:
        proc = running_instances[username]["proc"]
        if proc.poll() is None:
            return True

    env = os.environ.copy()
    env["PORT"] = str(port)
    env["CONFIG_FILE"] = f"bot_config_{username}.json"

    # Launch exact existing server.py in background
    proc = subprocess.Popen([sys.executable, "server.py"], env=env)
    running_instances[username] = {"port": port, "proc": proc}
    
    # Wait till instance starts listening
    for _ in range(25):
        time.sleep(0.3)
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/state", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
    return True

# ================= AUTH & ADMIN ROUTES =================

@app.route('/portal-login', methods=['GET', 'POST'])
def portal_login():
    if request.method == 'GET':
        return render_template('login.html', view="login")
    
    data = request.form or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    # Admin Login Check
    if username == ADMIN_USER and password == ADMIN_PASS:
        session["is_admin"] = True
        return redirect('/admin')

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT password, port, status FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()

    if not row:
        return render_template('login.html', view="login", error="User not registered. Contact admin.")

    db_pass, port, status = row
    if db_pass != password:
        return render_template('login.html', view="login", error="Invalid Password.")

    if status != 'ACTIVE':
        return render_template('login.html', view="login", error="Account Blocked. Contact admin.")

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
    c.execute("SELECT id, username, password, port, status, created_at FROM users ORDER BY id DESC")
    users = c.fetchall()
    conn.close()

    user_list = []
    for u in users:
        is_live = False
        if u[1] in running_instances and running_instances[u[1]]["proc"].poll() is None:
            is_live = True
        user_list.append({
            "id": u[0], "username": u[1], "password": u[2], "port": u[3],
            "status": u[4], "created_at": u[5], "is_live": is_live
        })

    return render_template('login.html', view="admin", users=user_list)

@app.route('/admin/add-user', methods=['POST'])
def admin_add_user():
    if not session.get("is_admin"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password:
        return redirect('/admin')

    port = get_next_port()
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password, port, status) VALUES (?, ?, ?, 'ACTIVE')", (username, password, port))
        conn.commit()
        conn.close()
    except Exception:
        pass

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

        # Agar block kiya toh background running process kill karega
        if new_status == 'BLOCKED' and uname in running_instances:
            try:
                running_instances[uname]["proc"].terminate()
            except Exception:
                pass
            running_instances.pop(uname, None)

    conn.close()
    return redirect('/admin')

# ================= REVERSE PROXY ROUTER =================

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def master_proxy_handler(path):
    # Admin & Portal routes bypass
    if path.startswith('portal-') or path.startswith('admin'):
        return Response("Not found", status=404)

    user = session.get("user")
    port = session.get("port")

    if not user or not port:
        return redirect('/portal-login')

    # Status re-verify
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT status FROM users WHERE username = ?", (user,))
    row = c.fetchone()
    conn.close()

    if not row or row[0] != 'ACTIVE':
        session.clear()
        return redirect('/portal-login')

    ensure_user_process(user, port)

    # Forward exact request to User's private Bot instance
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
    except Exception as e:
        return f"<h3>Private bot initializing for {user}... Please refresh in 5 seconds.</h3>", 503

if __name__ == '__main__':
    main_port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=main_port)
