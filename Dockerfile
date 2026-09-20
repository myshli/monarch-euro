FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DIR=/data

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# State (SQLite ledger, FX cache, Monarch session, rules.json) lives on a
# volume so re-deploying the image never re-imports old transactions.
VOLUME ["/data"]

ENTRYPOINT ["monarch-euro"]
CMD ["sync"]
