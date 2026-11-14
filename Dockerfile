FROM python:3.12-slim
WORKDIR /app
# PROXY_HOPS=1: Railway's proxy appends the real client address to X-Forwarded-For (the client's own
# entries sit to its left), so per-address limits use that entry and cannot be dodged with a fake header.
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/exchange.db PROXY_HOPS=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
COPY starter ./starter
EXPOSE 8000
# One process on purpose: the market lives in memory (SQLite on the volume is the replay log).
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-keep-alive 75"]
