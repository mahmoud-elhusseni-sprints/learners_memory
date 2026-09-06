FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e .

COPY alembic.ini ./
COPY alembic ./alembic
COPY docs ./docs
COPY scripts ./scripts

EXPOSE 8000
CMD ["uvicorn", "learner_memory.main:app", "--host", "0.0.0.0", "--port", "8000"]
