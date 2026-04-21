# AI-Admin Pro

Standalone-first SaaS platform for beauty salons and barbershops built with
Django, PostgreSQL, Celery, Redis, and OpenAI Function Calling.

## Local start

1. Create a virtual environment and install dependencies:
   `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`.
3. Run migrations:
   `python manage.py makemigrations bookings`
   `python manage.py migrate`
4. Start the server:
   `python manage.py runserver`

By default, local development uses SQLite so you can boot the project quickly
without PostgreSQL or Redis. Production settings are still compatible with
PostgreSQL and Redis.

## Local tests

- `pytest`
- `python manage.py test`

`pytest` uses `config.settings.test`, runs on SQLite, and does not require any
external CRM integration.

## Environment variables

Main variables are documented in `.env.example`.
