# 配件安裝系統 — 部署說明

## 專案結構

```
install-system/
├── backend/
│   ├── app.py               ← Flask 主程式（API + 通知）
│   ├── routes_frontend.py   ← 靜態頁面路由
│   ├── requirements.txt
│   └── .env.example         ← 環境變數範本
├── frontend/
│   └── public/
│       ├── index.html       ← 派工管理後台（Web）
│       └── installer.html   ← 施工人員端（Mobile）
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 快速啟動（本地開發）

### 方法一：直接執行

```bash
# 1. 進入後端目錄
cd backend

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 設定環境變數
cp .env.example .env
# 編輯 .env 填入你的 LINE Notify Token 和 SMTP 設定

# 4. 啟動（開發模式）
FLASK_ENV=development python app.py

# 5. 開啟後台
# http://localhost:5000
```

### 方法二：Docker Compose（推薦正式環境）

```bash
# 1. 複製環境變數
cp backend/.env.example .env

# 2. 編輯 .env（至少填 ADMIN_TOKEN 和 BASE_URL）
vim .env

# 3. 啟動
docker compose up -d

# 4. 查看 log
docker compose logs -f

# 停止
docker compose down
```

---

## 環境變數說明

| 變數名 | 必填 | 說明 |
|--------|------|------|
| `BASE_URL` | ✅ | 對外服務網址，用於產生施工連結，如 `https://install.yourcompany.com` |
| `ADMIN_TOKEN` | ✅ | 後台 API 認證 Token，請設定強密碼 |
| `LINE_NOTIFY_TOKEN` | 推薦 | LINE Notify 群組 Token |
| `SMTP_HOST` | 選填 | Email SMTP 主機（如 smtp.gmail.com） |
| `SMTP_USER` | 選填 | Gmail 帳號 |
| `SMTP_PASS` | 選填 | Gmail 應用程式密碼 |
| `MAX_PHOTO_MB` | 選填 | 照片大小上限，預設 10MB |
| `UPLOAD_FOLDER` | 選填 | 照片儲存路徑，預設 uploads/ |

---

## LINE Notify 設定步驟

1. 前往 https://notify-bot.line.me/zh_TW/
2. 登入 LINE 帳號 → 「管理登入服務」→「發行權杖」
3. 選擇要通知的群組，複製 Token
4. 將 Token 填入 `.env` 的 `LINE_NOTIFY_TOKEN`

---

## Gmail SMTP 設定步驟

1. Google 帳號 → 安全性 → 啟用兩步驟驗證
2. 應用程式密碼 → 選擇「郵件」→ 產生密碼
3. 將帳號與密碼填入 `.env`

---

## API 端點一覽

### 工單管理（需 X-Admin-Token 標頭）

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/orders` | 列出所有工單（支援 `?status=` 篩選） |
| POST | `/api/orders` | 建立工單 |
| GET | `/api/orders/:id` | 取得單一工單 |
| POST | `/api/orders/:id/assign` | 指派技師 |
| POST | `/api/orders/:id/approve` | 通過完工 |
| POST | `/api/orders/:id/reject` | 退回工單 |
| POST | `/api/orders/:id/recall` | 收回工單 |

### 施工人員（無需 Token）

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/orders/:id/arrive` | 記錄到場時間 |
| POST | `/api/orders/:id/submit` | 提交完工 |
| GET | `/api/orders/:id/photos` | 取得照片列表 |
| POST | `/api/orders/:id/photos` | 上傳照片（含自動加水印） |
| GET | `/api/photos/:filename` | 取得照片檔案 |

### 其他

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/users` | 列出人員 |
| POST | `/api/users` | 新增人員 |
| GET | `/api/export/excel` | 匯出 Excel |
| GET | `/api/health` | 健康檢查 |

---

## 正式部署建議

### 方案一：Fly.io（免費額度夠用）

```bash
# 安裝 flyctl
curl -L https://fly.io/install.sh | sh

# 登入
fly auth login

# 初始化（在專案目錄）
fly launch --name install-system

# 設定環境變數
fly secrets set ADMIN_TOKEN="your-strong-token"
fly secrets set LINE_NOTIFY_TOKEN="your-line-token"
fly secrets set BASE_URL="https://install-system.fly.dev"

# 部署
fly deploy
```

### 方案二：Railway（最簡單）

1. 連結 GitHub repo
2. 新增專案 → 選 GitHub repo
3. 在 Variables 頁面設定環境變數
4. 自動部署

### 方案三：自有 VPS（Ubuntu）

```bash
# 安裝 Docker
curl -fsSL https://get.docker.com | sh

# Clone 專案
git clone <your-repo> install-system
cd install-system

# 設定 .env
cp backend/.env.example .env
vim .env

# 啟動
docker compose up -d

# Nginx 反向代理（建議）
# /etc/nginx/sites-available/install-system
# server {
#     listen 80;
#     server_name install.yourcompany.com;
#     location / { proxy_pass http://127.0.0.1:5000; }
# }
```

---

## 工單流程

```
廠務建立工單 → 指派技師 → LINE/Email 通知（含施工連結）
    ↓
技師點連結 → 到場打卡 → 施工前拍照（環車記錄）
    ↓
完工拍照（依配件動態展開） → 數位簽名 → 提交完工
    ↓
廠務收到通知 → 後台審核照片 → 通過 or 退回
    ↓
匯出 Excel / 查看圖庫
```

---

## 安全注意事項

- `ADMIN_TOKEN` 請使用至少 32 字元的隨機字串
- 正式環境必須使用 HTTPS
- `uploads/` 目錄建議定期備份
- 照片路徑已做防路徑穿越處理
