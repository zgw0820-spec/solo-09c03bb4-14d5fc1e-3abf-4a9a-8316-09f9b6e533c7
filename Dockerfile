FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/data/app.db \
    MAX_POINTS=2000 \
    WORKER_ENABLED=1
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
