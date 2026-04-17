"""
配件安裝系統 — 後端主程式
Flask + SQLite + LINE Messaging API + 照片上傳
"""
import os
import uuid
import json
import secrets
import sqlite3
import logging
from functools import wraps
from datetime import datetime

import requests
from flask import Flask, request, abort, jsonify, send_file, g
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

# ── App 初始化 ─────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.getenv("ALLOWED_ORIGINS", "*")}})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ── 資料持久化路徑（Railway Volume 請設 DATA_DIR=/data）────
DATA_DIR      = os.getenv("DATA_DIR", ".")          # 持久 Volume 掛載點，預設當前目錄（本機開發）
DB_PATH       = os.path.join(DATA_DIR, "install.db")
UPLOAD_FOLDER = os.path.join(DATA_DIR, os.getenv("UPLOAD_FOLDER", "uploads"))

MAX_PHOTO_MB   = int(os.getenv("MAX_PHOTO_MB", "10"))
ADMIN_TOKEN    = os.getenv("ADMIN_TOKEN", "change-me-in-production")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TARGET_ID            = os.getenv("LINE_TARGET_ID", "")
SMTP_HOST      = os.getenv("SMTP_HOST", "")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASS      = os.getenv("SMTP_PASS", "")
ALLOWED_MIME   = {"image/jpeg", "image/png", "image/webp"}
BASE_URL       = os.getenv("BASE_URL", "http://localhost:5000")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── 登入 Session 管理（SQLite，跨 worker 共享）────────────

# ── 資料庫 ─────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            line_id   TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            role      TEXT NOT NULL CHECK(role IN ('factory','installer')),
            email     TEXT DEFAULT '',
            phone     TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS orders (
            order_id      TEXT PRIMARY KEY,
            source        TEXT DEFAULT '',
            car_no        TEXT NOT NULL,
            car_type      TEXT DEFAULT '',
            engine_no     TEXT DEFAULT '',
            location      TEXT DEFAULT '',
            install_date  TEXT DEFAULT '',
            items         TEXT DEFAULT '',
            note          TEXT DEFAULT '',
            installer_id  TEXT DEFAULT '',
            status        TEXT DEFAULT '待派工',
            reject_reason TEXT DEFAULT '',
            arrived_at    TEXT DEFAULT '',
            completed_at  TEXT DEFAULT '',
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS photos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id   TEXT NOT NULL,
            filename   TEXT NOT NULL,
            photo_type TEXT NOT NULL,
            uploaded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(order_id) REFERENCES orders(order_id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            line_id TEXT NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY(line_id, key)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL DEFAULT '',
            label      TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accessories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            photos     TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name  TEXT NOT NULL DEFAULT '',
            role          TEXT NOT NULL CHECK(role IN ('admin','factory','installer')),
            is_active     INTEGER DEFAULT 1,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS login_sessions (
            token        TEXT PRIMARY KEY,
            account_id   INTEGER NOT NULL,
            username     TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role         TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_orders_installer ON orders(installer_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_photos_order     ON photos(order_id);
        """
        )
        # 向下相容：舊資料庫補欄位
        for col, default in [("arrival_date", "''"), ("delivery_date", "''")]:
            try:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass
        conn.executescript("""
        """)
        # Seed 系統參數（只在首次建立時插入）
        conn.executemany(
            "INSERT OR IGNORE INTO settings(key,value,label) VALUES(?,?,?)",
            [
                ("order_front",  "B",        "前碼（最多4碼）"),
                ("order_middle", "2026",     "中碼（最多8碼）"),
                ("order_suffix_digits", "5", "後碼流水號位數"),
            ]
        )
        # Seed 預設配件（只在首次建立時插入）
        _seeds = [
            ("行車紀錄器（單鏡）", ["主機畫面", "GPS 位置"], 0),
            ("行車紀錄器（雙鏡）", ["主機畫面", "倒車畫面", "GPS 位置", "後鏡頭安裝位置"], 1),
            ("行車紀錄器（四鏡）", ["主機畫面", "倒車畫面", "左前鏡頭", "左後鏡頭", "GPS 位置"], 2),
            ("環景系統",           ["主機畫面", "倒車影像", "前鏡頭", "後鏡頭", "左鏡頭", "右鏡頭", "GPS 位置"], 3),
            ("安卓主機",           ["主機開機畫面", "後視鏡頭", "系統設定畫面"], 4),
            ("GPS 追蹤器",         ["安裝位置照", "訊號確認畫面"], 5),
            ("胎壓偵測器",         ["感應器安裝照", "顯示器畫面"], 6),
            ("其他配件",           ["安裝位置照"], 7),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO accessories(name,photos,sort_order) VALUES(?,?,?)",
            [(n, json.dumps(p, ensure_ascii=False), s) for n, p, s in _seeds]
        )
        # Seed 測試帳號
        conn.executemany(
            "INSERT OR IGNORE INTO users(line_id,name,role,email) VALUES(?,?,?,?)",
            [
                ("U_factory1", "廠務王大明", "factory", "factory@example.com"),
                ("U_install1", "技師陳大明", "installer", "install1@example.com"),
                ("U_install2", "技師林小華", "installer", "install2@example.com"),
            ]
        )
        # Seed 預設管理員帳號（admin / admin123）
        conn.execute(
            "INSERT OR IGNORE INTO accounts(username,password_hash,display_name,role) VALUES(?,?,?,?)",
            ("admin", generate_password_hash("admin123"), "系統管理員", "admin")
        )
        conn.commit()

init_db()

# ── 認證裝飾器 ─────────────────────────────────────────────
def _resolve_auth():
    """從 request 取得並驗證身份，回傳 user dict 或 None"""
    token = request.headers.get("X-Admin-Token") or request.args.get("token")
    if not token:
        return None
    # 向下相容：舊的 ADMIN_TOKEN（環境變數）
    if token == ADMIN_TOKEN:
        return {"id": 0, "username": "admin", "display_name": "Admin(ENV)", "role": "admin"}
    # SQLite-backed session（跨 gunicorn worker 共享）
    db = get_db()
    row = db.execute(
        "SELECT account_id,username,display_name,role FROM login_sessions WHERE token=?",
        (token,)
    ).fetchone()
    if row:
        return {"id": row["account_id"], "username": row["username"],
                "display_name": row["display_name"], "role": row["role"]}
    return None

def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _resolve_auth()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        g.current_user = user
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    """需要管理員權限"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _resolve_auth()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if user.get("role") != "admin":
            return jsonify({"error": "需要管理員權限"}), 403
        g.current_user = user
        return f(*args, **kwargs)
    return decorated

# ══════════════════════════════════════════════════════════════
#  API — 登入 / 帳號管理
# ══════════════════════════════════════════════════════════════

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"error": "請輸入帳號和密碼"}), 400
    db = get_db()
    acc = db.execute(
        "SELECT * FROM accounts WHERE username=? AND is_active=1", (username,)
    ).fetchone()
    if not acc or not check_password_hash(acc["password_hash"], password):
        return jsonify({"error": "帳號或密碼錯誤"}), 401
    token = secrets.token_hex(32)
    user_info = {
        "id": acc["id"], "username": acc["username"],
        "display_name": acc["display_name"], "role": acc["role"],
    }
    db.execute(
        "INSERT OR REPLACE INTO login_sessions(token,account_id,username,display_name,role) VALUES(?,?,?,?,?)",
        (token, acc["id"], acc["username"], acc["display_name"], acc["role"])
    )
    db.commit()
    logger.info(f"登入成功: {username} (role={acc['role']})")
    return jsonify({"ok": True, "token": token, "user": user_info})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = request.headers.get("X-Admin-Token")
    if token:
        db = get_db()
        row = db.execute("SELECT username FROM login_sessions WHERE token=?", (token,)).fetchone()
        if row:
            logger.info(f"登出: {row['username']}")
            db.execute("DELETE FROM login_sessions WHERE token=?", (token,))
            db.commit()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def auth_me():
    user = _resolve_auth()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(user)


@app.route("/api/auth/change-password", methods=["POST"])
@require_token
def change_password():
    data = request.get_json(force=True)
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if not old_pw or not new_pw:
        return jsonify({"error": "請提供舊密碼和新密碼"}), 400
    if len(new_pw) < 4:
        return jsonify({"error": "新密碼至少 4 個字元"}), 400
    db = get_db()
    acc = db.execute("SELECT * FROM accounts WHERE id=?", (g.current_user["id"],)).fetchone()
    if not acc or not check_password_hash(acc["password_hash"], old_pw):
        return jsonify({"error": "舊密碼錯誤"}), 401
    db.execute("UPDATE accounts SET password_hash=? WHERE id=?",
               (generate_password_hash(new_pw), g.current_user["id"]))
    db.commit()
    logger.info(f"密碼已變更: {g.current_user['username']}")
    return jsonify({"ok": True})


@app.route("/api/accounts", methods=["GET"])
@require_admin
def list_accounts():
    db = get_db()
    rows = db.execute(
        "SELECT id,username,display_name,role,is_active,created_at FROM accounts ORDER BY id"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/accounts", methods=["POST"])
@require_admin
def create_account():
    data = request.get_json(force=True)
    for f in ["username", "password", "display_name", "role"]:
        if not data.get(f):
            return jsonify({"error": f"缺少欄位: {f}"}), 400
    if data["role"] not in ("admin", "factory", "installer"):
        return jsonify({"error": "角色無效"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO accounts(username,password_hash,display_name,role) VALUES(?,?,?,?)",
            (data["username"].strip(), generate_password_hash(data["password"]),
             data["display_name"].strip(), data["role"])
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "此帳號已存在"}), 409
    return jsonify({"ok": True}), 201


@app.route("/api/accounts/<int:acc_id>", methods=["PUT"])
@require_admin
def update_account(acc_id):
    data = request.get_json(force=True)
    db = get_db()
    if not db.execute("SELECT 1 FROM accounts WHERE id=?", (acc_id,)).fetchone():
        return jsonify({"error": "帳號不存在"}), 404
    sets, params = [], []
    if data.get("display_name"):
        sets.append("display_name=?"); params.append(data["display_name"].strip())
    if data.get("role") and data["role"] in ("admin", "factory", "installer"):
        sets.append("role=?"); params.append(data["role"])
    if data.get("password"):
        sets.append("password_hash=?"); params.append(generate_password_hash(data["password"]))
    if "is_active" in data:
        sets.append("is_active=?"); params.append(1 if data["is_active"] else 0)
    if not sets:
        return jsonify({"error": "無更新資料"}), 400
    params.append(acc_id)
    db.execute(f"UPDATE accounts SET {','.join(sets)} WHERE id=?", params)
    db.commit()
    # 更新記憶體中的 session
    acc = db.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if acc:
        for tk, info in list(_sessions.items()):
            if info["id"] == acc_id:
                _sessions[tk] = {"id": acc["id"], "username": acc["username"],
                                 "display_name": acc["display_name"], "role": acc["role"]}
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:acc_id>", methods=["DELETE"])
@require_admin
def delete_account(acc_id):
    if g.current_user["id"] == acc_id:
        return jsonify({"error": "無法刪除自己的帳號"}), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM accounts WHERE id=?", (acc_id,)).fetchone():
        return jsonify({"error": "帳號不存在"}), 404
    db.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    db.commit()
    for tk, info in list(_sessions.items()):
        if info["id"] == acc_id:
            del _sessions[tk]
    return jsonify({"ok": True})


# ── 通知模組（LINE Messaging API）──────────────────────────
def line_push(to: str, text: str) -> bool:
    """
    透過 LINE Messaging API 推送訊息給指定 userId / groupId / roomId。
    文件：https://developers.line.biz/en/reference/messaging-api/#send-push-message
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定，跳過 LINE 推送")
        return False
    if not to:
        logger.warning("line_push: 缺少 to（userId/groupId），略過")
        return False
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "to": to,
                "messages": [{"type": "text", "text": text[:4999]}],  # LINE 單則上限 5000 字
            },
            timeout=5,
        )
        if resp.status_code >= 400:
            logger.error(f"LINE push 失敗 [{resp.status_code}]: {resp.text}")
            return False
        logger.info(f"LINE push 已送出 → {to}")
        return True
    except requests.RequestException as e:
        logger.error(f"LINE Messaging API 失敗: {e}")
        return False

def line_broadcast(text: str) -> bool:
    """廣播給所有好友（無特定目標時使用）"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定，跳過 LINE 廣播")
        return False
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"messages": [{"type": "text", "text": text[:4999]}]},
            timeout=5,
        )
        if resp.status_code >= 400:
            logger.error(f"LINE broadcast 失敗 [{resp.status_code}]: {resp.text}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"LINE Messaging API 廣播失敗: {e}")
        return False

def notify_line(message: str, to: str = "") -> bool:
    """
    發送 LINE 通知。
    - 若提供 to（userId/groupId/roomId）則推送給該目標
    - 否則推送給 LINE_TARGET_ID（後台預設群組/個人）
    - 若兩者皆無，退而採 broadcast
    """
    target = to or LINE_TARGET_ID
    if target:
        return line_push(target, message)
    return line_broadcast(message)

def notify_email(to_email: str, subject: str, body: str):
    """透過 SMTP 發送 Email"""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, to_email]):
        logger.warning(f"Email 設定不完整，跳過 Email 通知 (to={to_email})")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart()
        msg["From"]    = SMTP_USER
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info(f"Email 已送出 → {to_email}")
        return True
    except Exception as e:
        logger.error(f"Email 發送失敗: {e}")
        return False

def send_order_notification(order_id: str, event: str, extra: str = ""):
    """統一發送工單事件通知"""
    db = get_db()
    row = db.execute(
        "SELECT o.*, u.name as installer_name, u.email as installer_email "
        "FROM orders o LEFT JOIN users u ON o.installer_id=u.line_id "
        "WHERE o.order_id=?", (order_id,)
    ).fetchone()
    if not row:
        return

    installer_url = f"{BASE_URL}/installer/{order_id}"
    events = {
        "assigned":   (f"\n📦 新工單指派\n工單：{order_id}\n車牌：{row['car_no']}\n配件：{row['items']}\n地點：{row['location']}\n➜ {installer_url}", "【新工單】"),
        "submitted":  (f"\n🔍 工單待審核\n工單：{order_id}\n車牌：{row['car_no']}\n技師：{row['installer_name']}", "【待審核】"),
        "approved":   (f"\n✅ 工單已通過\n工單：{order_id}\n車牌：{row['car_no']}", "【完工通過】"),
        "rejected":   (f"\n❌ 工單退回\n工單：{order_id}\n原因：{extra}", "【工單退回】"),
        "recalled":   (f"\n🔄 工單已收回\n工單：{order_id}", "【工單收回】"),
    }
    line_msg, email_subj = events.get(event, ("", ""))
    if not line_msg:
        return

    # LINE Messaging API
    if event == "assigned":
        # 直接推送到被指派的技師 LINE userId（users.line_id 欄位）
        installer_line_id = row["installer_id"]
        if installer_line_id:
            notify_line(line_msg, to=installer_line_id)
        # 同步通知後台預設群組（若有）
        if LINE_TARGET_ID and LINE_TARGET_ID != installer_line_id:
            notify_line(line_msg)
    else:
        # 其它事件推送到後台預設目標（廠務群組/個人）
        notify_line(line_msg)

    # Email：指派時通知技師，提交/審核結果通知廠務
    if event == "assigned" and row["installer_email"]:
        notify_email(
            row["installer_email"], f"{email_subj} {order_id}",
            f"<h2>新工單：{order_id}</h2><p>車牌：{row['car_no']}<br>配件：{row['items']}<br>地點：{row['location']}</p>"
            f"<p><a href='{installer_url}' style='padding:12px 24px;background:#185FA5;color:#fff;text-decoration:none;border-radius:8px'>前往施工頁面</a></p>"
        )
    elif event in ("submitted", "approved", "rejected"):
        factory_users = db.execute("SELECT email FROM users WHERE role='factory' AND email!=''").fetchall()
        for fu in factory_users:
            notify_email(fu["email"], f"{email_subj} {order_id}", f"<h2>{email_subj}</h2><p>工單：{order_id}</p><p>{extra}</p>")

# ── 照片水印 ───────────────────────────────────────────────
def add_watermark(img_path: str, order_id: str, car_no: str):
    try:
        img  = Image.open(img_path).convert("RGB")
        # 縮圖至最長邊 1600px
        img.thumbnail((1600, 1600), Image.LANCZOS)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except OSError:
            font = ImageFont.load_default(size=24)
        ts   = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = f"ORDER:{order_id}  CAR:{car_no}  {ts}"
        # 底色增強可讀性
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y  = 16, img.height - th - 24
        draw.rectangle([x-6, y-4, x+tw+6, y+th+4], fill=(0, 0, 0, 160))
        draw.text((x, y), text, fill=(255, 255, 255), font=font)
        img.save(img_path, "JPEG", quality=88, optimize=True)
        logger.info(f"水印已加至 {img_path}")
    except Exception as e:
        logger.warning(f"add_watermark 失敗: {e}")

# ══════════════════════════════════════════════════════════════
#  API — 工單管理
# ══════════════════════════════════════════════════════════════

@app.route("/api/orders", methods=["GET"])
@require_token
def list_orders():
    db = get_db()
    conditions, params = [], []

    if v := request.args.get("status"):
        conditions.append("o.status=?"); params.append(v)
    if v := request.args.get("car_no"):
        conditions.append("o.car_no LIKE ?"); params.append(f"%{v}%")
    if v := request.args.get("order_id"):
        conditions.append("o.order_id LIKE ?"); params.append(f"%{v}%")
    if v := request.args.get("date_from"):
        conditions.append("DATE(o.created_at)>=?"); params.append(v)
    if v := request.args.get("date_to"):
        conditions.append("DATE(o.created_at)<=?"); params.append(v)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""SELECT o.order_id,o.source,o.car_no,o.car_type,o.engine_no,
                     o.location,o.install_date,o.arrival_date,o.delivery_date,
                     o.items,o.note,o.status,
                     o.reject_reason,o.arrived_at,o.completed_at,o.created_at,
                     u.name as installer_name
              FROM orders o
              LEFT JOIN users u ON o.installer_id=u.line_id
              {where}
              ORDER BY o.created_at DESC"""
    rows = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/orders", methods=["POST"])
@require_token
def create_order():
    data     = request.get_json(force=True)
    required = ["car_no"]
    for f in required:
        if not data.get(f):
            return jsonify({"error": f"缺少必要欄位: {f}"}), 400

    order_id = generate_order_id()
    db       = get_db()
    db.execute(
        """INSERT INTO orders
           (order_id,source,car_no,car_type,engine_no,location,
            install_date,arrival_date,delivery_date,items,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (order_id, data.get("source",""), data["car_no"],
         data.get("car_type",""), data.get("engine_no",""),
         data.get("location",""), data.get("install_date",""),
         data.get("arrival_date",""), data.get("delivery_date",""),
         ",".join(data.get("items",[])), data.get("note",""))
    )
    db.commit()
    logger.info(f"建立工單 {order_id}")
    return jsonify({"order_id": order_id, "installer_url": f"{BASE_URL}/installer/{order_id}"}), 201


@app.route("/api/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    """施工人員端取得工單（無需 admin token，由 URL 做資源限定）"""
    db  = get_db()
    row = db.execute(
        "SELECT o.*,u.name as installer_name FROM orders o "
        "LEFT JOIN users u ON o.installer_id=u.line_id WHERE o.order_id=?",
        (order_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "工單不存在"}), 404
    return jsonify(dict(row))


@app.route("/api/orders/<order_id>", methods=["PUT"])
@require_token
def update_order(order_id):
    """更新工單基本資訊（不改狀態）"""
    data = request.get_json(force=True)
    db   = get_db()
    if not db.execute("SELECT 1 FROM orders WHERE order_id=?", (order_id,)).fetchone():
        return jsonify({"error": "工單不存在"}), 404
    db.execute(
        """UPDATE orders SET
           source=?, car_no=?, car_type=?, engine_no=?, location=?,
           install_date=?, arrival_date=?, delivery_date=?, items=?, note=?
           WHERE order_id=?""",
        (data.get("source",""), data.get("car_no",""),
         data.get("car_type",""), data.get("engine_no",""),
         data.get("location",""), data.get("install_date",""),
         data.get("arrival_date",""), data.get("delivery_date",""),
         ",".join(data.get("items",[])) if isinstance(data.get("items"), list)
             else data.get("items",""),
         data.get("note",""), order_id)
    )
    db.commit()
    logger.info(f"更新工單 {order_id}")
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/assign", methods=["POST"])
@require_token
def assign_order(order_id):
    data     = request.get_json(force=True)
    inst_name = data.get("installer_name","").strip()
    db        = get_db()
    inst      = db.execute(
        "SELECT line_id FROM users WHERE name=? AND role='installer'", (inst_name,)
    ).fetchone()
    if not inst:
        return jsonify({"error": "找不到此技師"}), 404
    db.execute(
        "UPDATE orders SET installer_id=?,status='待確認' WHERE order_id=?",
        (inst["line_id"], order_id)
    )
    db.commit()
    send_order_notification(order_id, "assigned")
    return jsonify({"ok": True, "status": "待確認"})


@app.route("/api/orders/<order_id>/arrive", methods=["POST"])
def order_arrive(order_id):
    db = get_db()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE orders SET status='施工中', arrived_at=? WHERE order_id=? AND status='待確認'",
        (ts, order_id)
    )
    db.commit()
    return jsonify({"ok": True, "arrived_at": ts})


@app.route("/api/orders/<order_id>/submit", methods=["POST"])
def submit_order(order_id):
    db  = get_db()
    row = db.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not row or row["status"] != "施工中":
        return jsonify({"error": "狀態不符，無法提交"}), 400
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE orders SET status='待審核', completed_at=? WHERE order_id=?",
        (ts, order_id)
    )
    db.commit()
    send_order_notification(order_id, "submitted")
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/approve", methods=["POST"])
@require_token
def approve_order(order_id):
    db = get_db()
    db.execute(
        "UPDATE orders SET status='已完成',reject_reason='' WHERE order_id=?", (order_id,)
    )
    db.commit()
    send_order_notification(order_id, "approved")
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/reject", methods=["POST"])
@require_token
def reject_order(order_id):
    reason = request.get_json(force=True).get("reason","未說明")
    db     = get_db()
    db.execute(
        "UPDATE orders SET status='退回',reject_reason=? WHERE order_id=?",
        (reason, order_id)
    )
    db.commit()
    send_order_notification(order_id, "rejected", reason)
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/recall", methods=["POST"])
@require_token
def recall_order(order_id):
    db = get_db()
    db.execute(
        "UPDATE orders SET installer_id='',status='待派工',reject_reason='' WHERE order_id=?",
        (order_id,)
    )
    db.commit()
    send_order_notification(order_id, "recalled")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════
#  API — 照片上傳
# ══════════════════════════════════════════════════════════════

@app.route("/api/orders/<order_id>/photos", methods=["GET"])
def list_photos(order_id):
    db   = get_db()
    rows = db.execute(
        "SELECT id,filename,photo_type,uploaded_at FROM photos WHERE order_id=? ORDER BY id",
        (order_id,)
    ).fetchall()
    result = []
    for r in rows:
        result.append({
            **dict(r),
            "url": f"/api/photos/{r['filename']}"
        })
    return jsonify(result)


@app.route("/api/orders/<order_id>/photos", methods=["POST"])
def upload_photo(order_id):
    if "file" not in request.files:
        return jsonify({"error": "未附上檔案"}), 400

    file = request.files["file"]
    photo_type = request.form.get("photo_type", "other")

    # MIME 驗證
    if file.mimetype not in ALLOWED_MIME:
        return jsonify({"error": "僅支援 JPG / PNG / WebP 圖片"}), 415

    # 大小限制
    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    if size_mb > MAX_PHOTO_MB:
        return jsonify({"error": f"圖片不可超過 {MAX_PHOTO_MB}MB"}), 413

    db  = get_db()
    row = db.execute("SELECT car_no FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not row:
        return jsonify({"error": "工單不存在"}), 404

    ext      = "jpg" if file.mimetype == "image/jpeg" else file.mimetype.split("/")[1]
    filename = f"{order_id}_{uuid.uuid4().hex[:8]}.{ext}"
    path     = os.path.join(UPLOAD_FOLDER, filename)
    file.save(path)

    add_watermark(path, order_id, row["car_no"])

    db.execute(
        "INSERT INTO photos(order_id,filename,photo_type) VALUES(?,?,?)",
        (order_id, filename, photo_type)
    )
    db.commit()
    logger.info(f"照片上傳 {filename} → {order_id} [{photo_type}]")
    return jsonify({"ok": True, "filename": filename, "url": f"/api/photos/{filename}"}), 201


@app.route("/api/photos/<filename>")
def serve_photo(filename):
    # 防路徑穿越
    safe = os.path.basename(filename)
    path = os.path.join(UPLOAD_FOLDER, safe)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    return send_file(path)


# ══════════════════════════════════════════════════════════════
#  API — 使用者
# ══════════════════════════════════════════════════════════════

@app.route("/api/users", methods=["GET"])
@require_token
def list_users():
    db   = get_db()
    rows = db.execute("SELECT line_id,name,role,email,phone,created_at FROM users").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@require_token
def create_user():
    data = request.get_json(force=True)
    for f in ["line_id","name","role"]:
        if not data.get(f):
            return jsonify({"error": f"缺少欄位: {f}"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users(line_id,name,role,email,phone) VALUES(?,?,?,?,?)",
            (data["line_id"], data["name"], data["role"],
             data.get("email",""), data.get("phone",""))
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "此 LINE ID 已存在"}), 409
    return jsonify({"ok": True}), 201


@app.route("/api/users/<path:line_id>", methods=["PUT"])
@require_token
def update_user(line_id):
    data = request.get_json(force=True)
    for f in ["name", "role"]:
        if not data.get(f):
            return jsonify({"error": f"缺少欄位: {f}"}), 400
    db = get_db()
    old = db.execute("SELECT * FROM users WHERE line_id=?", (line_id,)).fetchone()
    if not old:
        return jsonify({"error": "找不到此使用者"}), 404

    new_line_id = data.get("line_id", line_id).strip() or line_id

    if new_line_id != line_id:
        # LINE ID 變更 → 需遷移 PK
        if db.execute("SELECT 1 FROM users WHERE line_id=?", (new_line_id,)).fetchone():
            return jsonify({"error": "新的 LINE ID 已被使用"}), 409
        db.execute(
            "INSERT INTO users(line_id,name,role,email,phone,created_at) VALUES(?,?,?,?,?,?)",
            (new_line_id, data["name"], data["role"],
             data.get("email", ""), data.get("phone", ""), old["created_at"])
        )
        db.execute("UPDATE orders SET installer_id=? WHERE installer_id=?",
                   (new_line_id, line_id))
        db.execute("DELETE FROM users WHERE line_id=?", (line_id,))
    else:
        db.execute(
            "UPDATE users SET name=?, role=?, email=?, phone=? WHERE line_id=?",
            (data["name"], data["role"],
             data.get("email", ""), data.get("phone", ""), line_id)
        )
    db.commit()
    return jsonify({"ok": True, "line_id": new_line_id})


@app.route("/api/users/<line_id>", methods=["DELETE"])
@require_token
def delete_user(line_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE line_id=?", (line_id,)).fetchone():
        return jsonify({"error": "找不到此使用者"}), 404
    # 檢查是否有進行中工單
    active = db.execute(
        "SELECT COUNT(*) FROM orders WHERE installer_id=? AND status NOT IN ('已完成','退回')",
        (line_id,)
    ).fetchone()[0]
    if active:
        return jsonify({"error": f"此技師有 {active} 張進行中工單，請先收回後再刪除"}), 409
    db.execute("DELETE FROM users WHERE line_id=?", (line_id,))
    db.commit()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════
#  API — 匯出
# ══════════════════════════════════════════════════════════════

@app.route("/api/export/excel")
@require_token
def export_excel():
    try:
        import pandas as pd
        db  = get_db()
        df  = pd.read_sql(
            "SELECT order_id,source,car_no,car_type,engine_no,location,"
            "install_date,items,status,installer_id,reject_reason,arrived_at,"
            "completed_at,created_at FROM orders ORDER BY created_at DESC",
            db
        )
        df.columns = ["工單號","來源","車牌","車型","引擎號","地點","安裝日期",
                      "配件","狀態","技師ID","退回原因","到場時間","完工時間","建立時間"]
        path = "/tmp/orders_export.xlsx"
        df.to_excel(path, index=False)
        return send_file(path, as_attachment=True, download_name="工單匯出.xlsx")
    except ImportError:
        return jsonify({"error": "請安裝 pandas 和 openpyxl"}), 500


# ══════════════════════════════════════════════════════════════
#  API — 系統參數
# ══════════════════════════════════════════════════════════════

@app.route("/api/settings", methods=["GET"])
@require_token
def list_settings():
    db   = get_db()
    rows = db.execute("SELECT key,value,label,updated_at FROM settings ORDER BY key").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/settings/<key>", methods=["PUT"])
@require_token
def update_setting(key):
    value = request.get_json(force=True).get("value", "")
    db    = get_db()
    rows_affected = db.execute(
        "UPDATE settings SET value=?, updated_at=datetime('now') WHERE key=?",
        (str(value).strip(), key)
    ).rowcount
    db.commit()
    if rows_affected == 0:
        return jsonify({"error": "找不到此參數"}), 404
    logger.info(f"參數更新 {key}={value}")
    return jsonify({"ok": True, "key": key, "value": str(value).strip()})


def generate_order_id() -> str:
    """
    產生 前碼(≤4碼) + 中碼(≤8碼) + 後碼(流水號) 格式工單號
    例：前碼=B, 中碼=20260416, 後碼5位 → B2026041600001
    """
    db = get_db()
    def _get(key, default):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    front  = (_get("order_front", "B").strip() or "B")[:4]
    middle = (_get("order_middle", "2026").strip() or "")[:8]
    digits = max(1, min(10, int(_get("order_suffix_digits", "5") or 5)))

    prefix = f"{front}{middle}"
    pattern = f"{prefix}%"

    row = db.execute(
        "SELECT order_id FROM orders WHERE order_id LIKE ? ORDER BY order_id DESC LIMIT 1",
        (pattern,)
    ).fetchone()
    if row:
        try:
            seq = int(row["order_id"][len(prefix):]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:0{digits}d}"


# ══════════════════════════════════════════════════════════════
#  API — 配件管理
# ══════════════════════════════════════════════════════════════

@app.route("/api/accessories", methods=["GET"])
def list_accessories():
    """取得所有配件（含拍照清單），施工端與後台共用，無需 token"""
    db   = get_db()
    rows = db.execute(
        "SELECT id,name,photos,sort_order FROM accessories ORDER BY sort_order,id"
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["photos"] = json.loads(d["photos"])
        result.append(d)
    return jsonify(result)


@app.route("/api/accessories", methods=["POST"])
@require_token
def create_accessory():
    data = request.get_json(force=True)
    if not data.get("name","").strip():
        return jsonify({"error": "缺少配件名稱"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO accessories(name,photos,sort_order) VALUES(?,?,?)",
            (data["name"].strip(),
             json.dumps(data.get("photos", []), ensure_ascii=False),
             int(data.get("sort_order", 0)))
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "此配件名稱已存在"}), 409
    return jsonify({"ok": True}), 201


@app.route("/api/accessories/<int:acc_id>", methods=["PUT"])
@require_token
def update_accessory(acc_id):
    data = request.get_json(force=True)
    if not data.get("name","").strip():
        return jsonify({"error": "缺少配件名稱"}), 400
    db = get_db()
    db.execute(
        "UPDATE accessories SET name=?,photos=?,sort_order=? WHERE id=?",
        (data["name"].strip(),
         json.dumps(data.get("photos", []), ensure_ascii=False),
         int(data.get("sort_order", 0)),
         acc_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/accessories/<int:acc_id>", methods=["DELETE"])
@require_token
def delete_accessory(acc_id):
    db = get_db()
    db.execute("DELETE FROM accessories WHERE id=?", (acc_id,))
    db.commit()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════
#  健康檢查
# ══════════════════════════════════════════════════════════════

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ── 前端靜態頁面路由 ───────────────────────────────────────
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "public")

@app.route("/")
def index_page():
    return send_file(os.path.join(FRONTEND_DIR, "index.html"))

@app.route("/installer/<order_id>")
def installer_page(order_id):
    return send_file(os.path.join(FRONTEND_DIR, "installer.html"))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
