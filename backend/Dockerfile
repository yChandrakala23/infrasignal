FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kan && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/uploads /app/data/uploads/documents
# Migrations run automatically on start (app.db.init_db). The container refuses to start with CHANGE_ME secrets.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
