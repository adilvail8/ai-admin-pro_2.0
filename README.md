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
3. Run database migrations:
   `python manage.py migrate`
4. Create an admin user if needed:
   `python manage.py createsuperuser`
5. Start the server:
   `python manage.py runserver`

By default, local development uses SQLite so you can boot the project quickly
without PostgreSQL or Redis. Production settings are still compatible with
PostgreSQL and Redis.

## API

Base path: `/api/v1/`

- `POST /api/v1/auth/token/` - obtain JWT access/refresh pair
- `POST /api/v1/auth/token/refresh/` - refresh access token
- `GET /api/v1/auth/me/` - current authenticated user
- `GET /api/v1/memberships/` - list business memberships
- `GET /api/v1/bookings/` - list bookings available to the authenticated user
- `GET /api/v1/outbound-messages/` - list outbound messages available to the authenticated user

## Webhooks And Ops

- `/api/webhooks/messenger/`
- `/api/webhooks/telegram/<secret>/`
- `/api/webhooks/green-api/`
- `/api/webhooks/whatsapp/<business_id>/`
- `/api/webhooks/outbound-delivery/`
- `/api/health/`

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
