web: alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
worker: alembic upgrade head && python -m app.worker --loop 60
