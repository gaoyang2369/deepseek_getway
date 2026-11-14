FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY gateway ./gateway
COPY migrations ./migrations
COPY alembic.ini ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn gateway.main:create_app --factory --host 0.0.0.0 --port 8080 --workers 1 --no-access-log --no-proxy-headers --timeout-graceful-shutdown 200"]
