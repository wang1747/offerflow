FROM python:3.13-slim

WORKDIR /app

RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple/ -r requirements.txt

COPY backend/ ./backend/
COPY run.py .

ENV PYTHONPATH=/app/backend

EXPOSE 8001

CMD ["python", "run.py"]
