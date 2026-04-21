# AI-Admin Pro Architecture

## File layout

```text
ai-admin-pro/
  manage.py
  requirements.txt
  docs/
    architecture.md
  config/
    __init__.py
    urls.py
    celery.py
    settings/
      __init__.py
      base.py
      local.py
      production.py
  apps/
    bookings/
      __init__.py
      apps.py
      admin.py
      models.py
      services.py
      selectors.py
      ai_tools.py
      tasks.py
      migrations/
        __init__.py
```

## Architectural principles

- `Business` is the tenant root and owns mode selection, CRM credentials,
  AI settings, and business knowledge base.
- `Master`, `Service`, and `Booking` model the standalone scheduling domain.
- Fat Models, Thin Views:
  business rules live in model methods and the service layer, not in views.
- `services.py` contains use cases:
  free slots discovery, booking creation, and future Altegio sync facades.
- `selectors.py` should hold optimized read queries for dashboards and APIs.
- `ai_tools.py` exposes OpenAI Function Calling schemas and dispatchers.
- `tasks.py` is the Celery boundary for reminders, outbound sync, and retries.
- `config/settings/base.py` uses `django-environ` for PostgreSQL, Redis,
  OpenAI, and Altegio credentials.

## Runtime modes

- `ALTEGIO`:
  data is synchronized with Altegio/YClients through gateway services.
- `STANDALONE`:
  the booking engine is native and uses local scheduling logic only.

## Recommended next steps

1. Split settings into `base/local/production`.
2. Add Django REST Framework or Django Ninja for the public API.
3. Add PostgreSQL-backed migrations and admin configuration.
4. Add Celery tasks for reminders and CRM synchronization.
5. Cover overlap and slot-discovery logic with unit tests.

