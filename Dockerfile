# Optional container build (Render can use the native Python runtime instead)
FROM python:3.11.9-slim

WORKDIR /srv/adops
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && (playwright install --with-deps chromium || true)

COPY . .

ENV DATA_DIR=/data
VOLUME /data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
