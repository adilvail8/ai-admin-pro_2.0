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

- `Business` is the tenant root and owns AI settings and business
  knowledge base.
- `Master`, `Service`, and `Booking` model the standalone scheduling domain.
- Fat Models, Thin Views:
  business rules live in model methods and the service layer, not in views.
- `services.py` contains use cases:
  free slots discovery and booking creation.
- `selectors.py` should hold optimized read queries for dashboards and APIs.
- `ai_tools.py` exposes OpenAI Function Calling schemas and dispatchers.
- `tasks.py` is the Celery boundary for reminders and asynchronous jobs.
- `config/settings/base.py` uses `django-environ` for PostgreSQL, Redis,
  OpenAI, and environment-specific configuration.

## Recommended next steps

1. Add Django REST Framework or Django Ninja for the public API.
2. Generate and commit initial Django migrations.
3. Add Celery tasks for reminders and outbound notifications.
4. Add authentication and tenant-aware permissions.
5. Expand test coverage for edge cases and API flows.
