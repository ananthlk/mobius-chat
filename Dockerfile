FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uvicorn

COPY app ./app/
COPY frontend/index.html ./frontend/
COPY frontend/static ./frontend/static/
COPY config ./config/
COPY db ./db/

ENV PORT=8080
ENV PYTHONPATH=/app
EXPOSE 8080

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
