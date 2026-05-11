# AI Admin Pro - Project Handoff Notes

This README is written as a project memory for future Codex sessions and for the
integrator/owner of the product. It documents the current local project state,
the architecture, the business rules we already learned the hard way, and the
places where future work must be careful.

Important: as of 2026-05-12, the local working tree contains many feature
changes. This README may be pushed alone as a handoff document, while code
changes should be committed separately and intentionally.

## Project Snapshot

AI Admin Pro is a Django-based SaaS/standalone platform for salons,
barbershops, beauty studios, and similar service businesses.

The product goal is not to be "just GPT in Telegram". The system is designed as
an AI administrator with deterministic booking logic, business-specific data,
owner cabinet, superadmin cabinet, audit trail, and messenger integrations.

Core channels:

- Telegram, currently the most stable test channel.
- WhatsApp via Green-API, partially integrated and still needs more production
  testing.

Core users:

- Client: writes to Telegram/WhatsApp and books a service.
- Salon owner/admin: sees only their business, bookings, clients, services, and
  dialogs.
- Integrator/superadmin: sees all businesses, technical logs, AI logs, and
  system state.

Current test baseline at the time of this handoff:

```powershell
.\.venv\Scripts\python.exe -m pytest
# 241 passed
```

## What Already Works

- Multi-business data model with business-scoped admin access.
- Telegram webhook flow for real client dialogs.
- Green-API normalizer and webhook path foundation.
- Deterministic booking flow before AI fallback.
- `BookingSession` state machine for active booking context.
- Service switching resets stale booking context.
- Service/master compatibility guard.
- Date/time parsing for common Russian/Kazakh booking phrases.
- Language selection based on the latest user message, not sticky old history.
- Off-topic guard so the bot refuses homework, essays, coding, and other
  unrelated topics.
- Owner admin panel with scoped sidebar and dialogs.
- Manual reply from owner dialog, sent outward as the bot.
- Superadmin panel keeps global visibility.
- Audit logs, AI logs, inbound events, outbound delivery records.
- Voice/photo events are normalized and should not crash the bot, but real STT
  or vision is not implemented yet.

## Key Product Principle

The bot must feel like a salon administrator, not a generic LLM.

That means:

- Do not answer unrelated questions.
- Do not invent services, masters, prices, dates, or policies.
- Prefer short operational replies.
- Booking logic should be deterministic wherever possible.
- AI is allowed to phrase things, but must not become the source of truth.

## Architecture Map

Main app:

```text
apps/bookings/
```

Important files:

- `models.py` - business entities, clients, bookings, outbound messages, audit,
  conversation history, booking sessions.
- `webhooks.py` - main inbound message processing and deterministic booking
  flow.
- `normalizers.py` - converts Telegram/WhatsApp webhook payloads into one
  internal event shape.
- `session_state.py` - helper layer for `BookingSession`: selected service,
  slots, state reset, selected slot.
- `services.py` - booking creation, status updates, slot lookup.
- `ai_manager.py` - AI manager, language inference, prompt building, fallback
  and handoff decisions.
- `ai/prompt_builder.py` - prompt layer and AI behavior rules.
- `tasks.py` - outbound dispatch, reminders, follow-ups, handoff notifications,
  maintenance tasks.
- `transports.py` - Telegram/WhatsApp/internal outbound transports.
- `views.py` - HTTP API/webhook views and dispatch entrypoints.
- `admin.py` - Django/Unfold admin customization for owner and superadmin.

Supporting files:

- `config/settings/base.py` - Unfold/Jazzmin-style admin settings, Celery
  queues, API settings, CORS, installed apps.
- `templates/admin/index.html` - owner dashboard shell.
- `templates/admin/bookings/conversationmessage/inbox.html` - owner dialog UI.
- `apps/bookings/static/bookings/css/owner_admin.css` - owner admin styling.
- `tests/test_bookings.py` - primary test suite for booking/admin behavior.
- `tests/test_language_preferences.py` - language switching tests.
- `tests/test_normalizers.py` - Telegram/WhatsApp event normalization tests.

## Message Flow

High-level inbound flow:

```text
Messenger webhook
  -> normalize event
  -> resolve business
  -> resolve/create client
  -> store inbound event/message
  -> load BookingSession
  -> deterministic booking/FAQ/guard logic
  -> AI only if deterministic layer cannot answer safely
  -> create OutboundMessage
  -> dispatch through Telegram or Green-API transport
  -> store assistant message
  -> audit/log delivery result
```

The most important design decision: `ai_manager` should receive clean business
context and recent conversation, not raw provider JSON and not unlimited
history.

## Internal Event Normalization

The normalizer layer exists because Telegram and WhatsApp send very different
payloads.

Internal event should carry:

- source/channel: `telegram` or `whatsapp`.
- event type: `text`, `voice`, `image`, `service`, `unsupported`.
- business id.
- client identity.
- message text/caption/file metadata.
- raw payload for debugging.

Expected behavior:

- Text goes into booking/AI flow.
- Voice/image should get a deterministic "please write text" style fallback
  unless STT/vision is implemented later.
- WhatsApp service events, delivery statuses, and instance noise should return
  200 without invoking AI.

## Booking Flow Rules

The booking flow is the heart of the project. Do not casually replace it with
AI-only behavior.

Required rules:

- Detect explicit service from current message first.
- If service changes, clear stale session fields: service/master/date/slot.
- A selected master must be compatible with the selected service.
- Unknown master names must not be accepted as real masters.
- Service slots must come from real active services and active masters.
- Dates in the past must be rejected deterministically.
- Time-only messages while waiting for date should ask for the date, not fall
  into AI fallback.
- Short confirmations like `да`, `ок`, `запишите`, `подтверждаю` must be
  handled by session state, not by generic AI.
- After booking is created, short follow-up messages should not resurrect old
  booking intents.
- Confirmed booking text must match actual booking status.
- A booking created by the bot is currently intended to be `CONFIRMED`, not
  pending human approval.

Known examples we fixed or guarded against:

- Year parsed as time, e.g. `2026` becoming `20:26`.
- Old Kazakh/Russian language preference overriding the latest message.
- User switches from haircut to lashes and old master/service leaks into the
  new booking.
- User asks FAQ like price/master and bot incorrectly starts booking flow.
- User asks unrelated seminar/homework questions and bot answers as ChatGPT.
- User types an invented master name and bot accepts it.

## Language Logic

Rules:

- Response language is based on the latest user message.
- Explicit language request in the latest message has top priority.
- If latest message has a clear RU/KZ signal, use that language immediately.
- Conversation history is only fallback for neutral messages like `1`, `10:00`,
  `да`.
- Do not explain language switching.
- Do not write long "I can speak Russian/Kazakh" disclaimers.
- Dates and service names should be localized as much as possible.

## AI Behavior Rules

The AI is a helper, not the business database.

It must not:

- invent masters;
- invent services;
- invent prices;
- invent available time;
- answer homework/seminars/essays/coding;
- reveal prompts or discuss system instructions;
- continue off-topic after refusal.

Preferred style:

- short;
- natural;
- calm;
- administrator-like;
- no generic phrases like "do not hesitate to contact us" unless needed.

Good example:

```text
Записала: стрижка и борода, 13 мая 10:00, мастер Азамат.
```

Bad example:

```text
Если у вас возникнут какие-либо дополнительные вопросы, пожалуйста, не
стесняйтесь обращаться, я всегда рад помочь.
```

## Owner Admin Panel

Owner panel is a business cabinet, not a technical Django admin.

Owner should see only their business.

Current intended owner sidebar:

```text
Записи
  - Бронирования
  - Клиенты
  - Мастера
  - Услуги
  - Категории
  - Настройки салона

Переписка
  - Диалоги
```

Owner should not see:

- raw `InboundEvent`;
- raw `OutboundMessage`;
- noisy `AuditLog`;
- global businesses;
- AI logs;
- other salons.

Dialog UI:

- client messages should be grey and on the left;
- bot/admin messages should be green and on the right;
- owner replies from the dialog input;
- reply is sent outward through the bot channel;
- filters: all, active, needs attention;
- labels: waiting for reply, no reply for 2h+;
- mode badge: bot/manual.

## Superadmin Panel

Superadmin is the integrator view.

Superadmin should see:

- all businesses;
- all bookings;
- all clients;
- all services/masters/categories;
- outbound messages;
- inbound events;
- audit logs;
- AI logs;
- users.

Audit should hide technical noise by default:

- `outbound_submitted`;
- `outbound_reply_queued`.

Technical audit can still be exposed explicitly when debugging, for example
through a query parameter such as:

```text
?show_technical=1
```

## Data Retention And Database Hygiene

The project currently stores a lot of data:

- `ConversationMessage`;
- `InboundEvent`;
- `OutboundMessage`;
- `AuditLog`;
- `AIInteractionLog`.

This is useful during development but can become noisy in production.

Suggested future retention policy:

- Keep `Booking` permanently.
- Keep full `ConversationMessage` for 30-90 days.
- Keep raw `InboundEvent` for 7-14 days.
- Keep technical audit/outbound delivery noise for 14-30 days.
- Keep important audit events longer: booking created/cancelled, manual reply,
  handoff, delivery failures.
- Add a management command like `prune_old_events`.
- Consider a future `ConversationThread` summary table for long-term dialog
  history.

## Reminders And Background Jobs

The codebase has Celery queues for:

- outbound messages;
- reminders;
- follow-ups;
- handoff notifications;
- maintenance.

Configured conceptual queues:

- `messages`;
- `ai_processing`;
- `maintenance`.

Before production, verify:

- Redis is running;
- worker processes are running;
- beat scheduler is running;
- `CELERY_TASK_ALWAYS_EAGER=False` in production;
- reminder/follow-up periodic tasks actually fire.

## Local Development Commands

Typical local start from the project directory:

```powershell
cd F:\django-sprint4-main\ai-admin-pro_2.0-main
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Run Django check:

```powershell
.\.venv\Scripts\python.exe manage.py check
```

Admin URL:

```text
http://127.0.0.1:8000/secure-admin/
```

Telegram webhook pattern:

```text
/api/v1/webhooks/telegram/<business_id>/<secret>/
```

WhatsApp/Green-API webhook pattern:

```text
/api/v1/webhooks/whatsapp/<business_id>/
```

Health check:

```text
/api/v1/health/
```

## Production Notes

Before first real salon:

- Rent a server or deploy to a managed platform.
- Use PostgreSQL, not SQLite.
- Use Redis for Celery.
- Run migrations intentionally.
- Configure domain and HTTPS.
- Configure Telegram webhook.
- Configure OpenAI API key.
- Configure Green-API instance only after more tests.
- Configure backups.
- Configure log rotation.
- Configure `.env` with real secrets.
- Do not commit real tokens.

Minimum production `.env` categories:

- Django secret key;
- database URL or DB host/user/password/name;
- allowed hosts;
- CSRF trusted origins;
- OpenAI key;
- Telegram bot token/secret;
- Green-API instance/token/shared secret;
- Redis/Celery broker;
- admin/handoff destination;
- timezone.

Backups:

- database backups are mandatory;
- daily backup is the minimum;
- test restore before selling to real clients;
- keep secrets out of backups where possible.

## Green-API Notes

Telegram is currently the cleaner channel.

Green-API/WhatsApp still needs focused P0 testing:

- incoming text payload shape;
- image/audio/service payloads;
- delivery/status noise;
- duplicate webhooks;
- `chatId` format with `@c.us`;
- outbound `sendMessage` error 466;
- Cyrillic/Kazakh text encoding;
- whether the instance is authorized and stable.

Expected architecture:

- Green-API-specific parsing belongs in `normalizers.py`.
- Core booking logic should not care whether the source is Telegram or WhatsApp.
- Transport-specific failures should be visible in `OutboundMessage` and
  superadmin logs.

## Known Risks And TODO

High priority:

- Fully test Green-API on real device/account.
- Verify reminders/follow-ups end-to-end with Celery worker and beat.
- Add retention/pruning command for noisy logs and raw events.
- Continue reducing AI-ish phrasing without weakening safety.
- Verify owner dialog UX in browser after each admin change.
- Prepare production server, backups, `.env`, migrations, and deployment runbook.

Medium priority:

- Better owner dashboard metrics.
- More natural RU/KZ localization and date phrasing.
- Voice message transcription through Whisper or similar.
- Image/caption support for nail/hair reference photos.
- Business-specific service aliases and FAQ.
- Explicit manual/bot mode switch in dialogs.

Low priority:

- Clean duplicate historical admin helper definitions.
- Split large `webhooks.py` into smaller modules.
- Add a formal conversation thread/summary model.

## Rules For Future Codex Sessions

These are the important rules I do not want future me to forget:

- Do not push code unless explicitly asked.
- If the user asks for README-only, commit/push only README.
- Do not hide bugs with prompt changes when deterministic logic is needed.
- Every booking-flow fix should get a test.
- Prefer targeted tests first, then full pytest.
- Do not let AI invent business facts.
- Do not let old conversation context override current session state.
- Do not show owner technical tables just because Django admin can.
- Keep superadmin powerful, owner simple.
- Be careful with encoding: PowerShell may display Cyrillic as mojibake while
  files are still valid UTF-8.
- The project may have a dirty git tree; never revert unrelated user/code
  changes.
- Use `apply_patch` for file edits.

## Git Hygiene For This Handoff

The intended README-only handoff workflow:

```powershell
git status --short
git add README.md
git diff --cached -- README.md
git commit -m "docs: add project handoff notes"
git push origin <current-branch>
```

Before committing, verify no code files are staged:

```powershell
git diff --cached --name-only
```

Expected staged file for this task:

```text
README.md
```
