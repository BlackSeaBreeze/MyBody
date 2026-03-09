# MyBody — образ для Cloud Run
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
ENV PYTHONPATH=/app

# Cloud Run ожидает порт из переменной PORT (по умолчанию 8080)
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
