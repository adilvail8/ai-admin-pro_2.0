#!/bin/sh
set -eu

role="${1:-web}"
shift || true

echo "Checking PostgreSQL connectivity..."
python manage.py check_postgres

case "$role" in
  web)
    echo "Applying migrations..."
    python manage.py migrate --noinput

    echo "Collecting static files..."
    python manage.py collectstatic --noinput

    echo "Starting Gunicorn..."
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${GUNICORN_WORKERS:-3}" \
      --timeout "${GUNICORN_TIMEOUT:-60}"
    ;;
  worker)
    echo "Starting Celery worker..."
    queues="${1:-${CELERY_WORKER_QUEUES:-messages}}"
    concurrency="${2:-${CELERY_WORKER_CONCURRENCY:-2}}"
    exec celery -A config.celery:app worker \
      --loglevel="${CELERY_LOGLEVEL:-info}" \
      --queues="$queues" \
      --concurrency="$concurrency"
    ;;
  beat)
    echo "Starting Celery beat..."
    exec celery -A config.celery:app beat --loglevel="${CELERY_LOGLEVEL:-info}"
    ;;
  flower)
    echo "Starting Flower..."
    flower_args="--port=${FLOWER_PORT:-5555}"
    if [ -n "${FLOWER_URL_PREFIX:-}" ]; then
      flower_args="$flower_args --url_prefix=${FLOWER_URL_PREFIX}"
    fi
    if [ -n "${FLOWER_BASIC_AUTH:-}" ]; then
      exec celery -A config.celery:app flower \
        $flower_args \
        --basic_auth="${FLOWER_BASIC_AUTH}"
    fi
    exec celery -A config.celery:app flower $flower_args
    ;;
  *)
    exec "$role" "$@"
    ;;
esac
