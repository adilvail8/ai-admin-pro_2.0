# AI-Admin Pro

Standalone-first SaaS platform for beauty salons and barbershops built with
Django, PostgreSQL, Celery, Redis, OpenAI Function Calling, and a protected
JWT API for operator and owner interfaces.

## What Is Included

- Standalone booking engine with masters, services, clients, and bookings
- Webhook layer for Telegram, WhatsApp/Green-API, and outbound delivery callbacks
- Outbound messaging pipeline with `queued/submitted/delivered/failed/dead_letter`
- AI assistant manager with dynamic business context and interaction logging
- Audit trail for booking, messaging, and handoff events
- JWT-protected API under `/api/v1/`

## Local Start

1. Create a virtual environment and install dependencies:
   `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`
3. Create a dedicated PostgreSQL database and user for this project
4. Check PostgreSQL connectivity:
   `python manage.py check_postgres`
5. Run database migrations:
   `python manage.py migrate`
6. Create an admin user if needed:
   `python manage.py createsuperuser`
7. Start the server:
   `python manage.py runserver`

The main application now uses PostgreSQL-first settings. Tests still run on
SQLite for speed and isolation.

## API

Base path: `/api/v1/`

- `POST /api/v1/auth/token/` - obtain JWT access/refresh pair
- `POST /api/v1/auth/token/refresh/` - refresh access token
- `GET /api/v1/auth/me/` - current authenticated user
- `GET /api/v1/memberships/` - list business memberships
- `GET /api/v1/bookings/` - list bookings available to the authenticated user
- `GET /api/v1/outbound-messages/` - list outbound messages available to the authenticated user

## Webhooks And Ops

- `/api/v1/webhooks/messenger/`
- `/api/v1/webhooks/telegram/<secret>/`
- `/api/v1/webhooks/green-api/`
- `/api/v1/webhooks/whatsapp/<business_id>/`
- `/api/v1/webhooks/outbound-delivery/`
- `/api/v1/health/`

## Local Tests

- `pytest`
- `python manage.py test`

`pytest` uses `config.settings.test`, runs on SQLite, and does not require any
external CRM integration.

## Dependency Strategy

- `requirements.txt` stores supported dependency ranges for development
- `requirements-lock.txt` stores a frozen environment for reproducible setup

## Environment Variables

Main variables are documented in `.env.example`.

Recommended PostgreSQL setup:

- create a dedicated database user, for example `adil_admin`
- grant that user access only to the project database
- do not use the shared superuser `postgres` for the Django app itself

## Docker Stack

Files included:

- `Dockerfile`
- `docker-compose.yml`
- `docker/entrypoint.sh`
- `docker/postgres/initdb/01-create-app-db.sh`

How to run:

1. Copy `.env.example` to `.env`
2. In `.env`, for Docker set:
   `DB_HOST=db`
   `CELERY_BROKER_URL=redis://redis:6379/0`
   `CELERY_RESULT_BACKEND=redis://redis:6379/0`
   `CELERY_TASK_ALWAYS_EAGER=False`
   and fill in all passwords/secrets
2. Start the stack:
   `docker compose up --build`
3. Check that Django sees PostgreSQL:
   `docker compose exec web python manage.py check_postgres`

Notes:

- stack includes `db`, `redis`, `web`, `worker_messages`, `worker_ai`, `worker_maintenance`, and `beat`
- `web` waits for healthy PostgreSQL and Redis, then runs `check_postgres`, `migrate`, and starts `gunicorn`
- `worker_messages` handles outbound delivery, reminders, follow-ups, and handoff notifications from the `messages` queue
- `worker_ai` is reserved for `ai_processing` tasks, so slow LLM work does not block messaging
- `worker_maintenance` handles `maintenance` tasks such as history pruning and reminder scanning
- `beat` runs Celery Beat for scheduled reminders/follow-ups
- PostgreSQL data is stored in the named volume `postgres_data`
- static and media directories are stored in named volumes so admin assets survive container restarts
- all passwords and secrets are loaded from `.env`

## Celery Queues

Configured queues:

- `messages` - outbound messages, reminders, follow-ups, handoff notifications
- `ai_processing` - AI-heavy background processing
- `maintenance` - maintenance tasks such as conversation pruning and periodic scans

Current routing is defined in [F:\django-sprint4-main\ai-admin-pro_2.0-main\config\settings\base.py](F:\django-sprint4-main\ai-admin-pro_2.0-main\config\settings\base.py).

Operational checks:

- `GET /api/v1/health/` now returns:
  - DB status
  - broker/scheduler readiness
  - eager-mode warning
  - configured Celery queue routes
