FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt

COPY main.py .

ENV PORT=5000
EXPOSE 5000

CMD gunicorn -b 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 300 main:app
