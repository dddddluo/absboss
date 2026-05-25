FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml uv.lock alembic.ini ./
COPY absbot ./absbot
COPY alembic ./alembic
COPY assets ./assets

RUN pip install --no-cache-dir uv && \
    uv export --frozen --no-dev --no-hashes -o /tmp/requirements.txt && \
    pip install --no-cache-dir -r /tmp/requirements.txt
RUN mkdir -p logs backups

CMD ["python", "-m", "absbot.main"]
