FROM python:3.12-slim

WORKDIR /app

# 系統依賴（Pillow 字型支援）
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Python 依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY app.py .
COPY frontend/ ./frontend/

# 建立上傳與資料目錄
RUN mkdir -p uploads

EXPOSE 5000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120"]
