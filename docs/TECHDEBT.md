# Tech Debt

Список технического долга, накопленного по результатам ревизии 2026-05-12.
Обновляется по мере появления / закрытия пунктов.

---

## 🚨 До запуска WhatsApp (безопасность)

### 1. ~~`green_api_webhook` — частичная валидация `business_id`~~ — **ЗАКРЫТО 2026-05-17**

Полностью закрыто в этой сессии: per-business credentials через
`Business.green_api_instance_id/api_token/api_url` (миграция 0020),
inbound lookup по `instanceData.idInstance`, cross-check URL ↔ payload,
outbound transport принимает business и берёт его creds. Подмена
business_id в payload больше невозможна — он игнорируется.

**Остаточные мелкие долги:**
- `api_token` хранится plaintext в БД. Шифрование (django-fernet-fields
  или KMS) — делать после деплоя.
- Глобальные `GREEN_API_INSTANCE_ID/API_TOKEN/URL` в env оставлены как
  fallback с warning'ом. Hard-fail когда все живые salons мигрируют
  на per-business creds.
- `GREEN_API_BUSINESS_IDS` whitelist оставлен как back-stop для
  `messenger_webhook` (legacy internal-payload entry point). Green-API
  fallback в `green_api_webhook` закрыт 2026-05-17.
- `messenger_webhook` (`/api/v1/webhooks/messenger/`) — выяснить нужен
  ли в проде. Если нет — закрыть + удалить `GREEN_API_BUSINESS_IDS` и
  `validate_green_api_business_id` целиком. Если да — заменить
  whitelist на tenant-scoped токены.
- Health-check на смешанное состояние (часть Business с creds, часть
  без) — отдельной задачей.

### 12. ~~Per-business Telegram credentials~~ — **ЗАКРЫТО 2026-05-18**

Зеркало пункта 1 (Green-API) для Telegram. Поля
`Business.telegram_bot_token` и `Business.telegram_webhook_secret`
(миграция 0021 + partial UniqueConstraint на secret). Inbound
`telegram_webhook(business_id, secret)` пропускает запрос только
если пара `(id, secret)` совпадает с одной записью Business (либо
если secret совпадает с глобальным `TELEGRAM_WEBHOOK_SECRET` —
deprecated fallback). Outbound `TelegramTransport(business=…)` берёт
`bot_token` из Business либо global fallback с warning'ом. Логика
полностью симметрична `WhatsAppTransport`.

**Остаточный долг (общий с пунктом 1):**
- `telegram_bot_token` plaintext в БД — закрыть шифрованием вместе с
  `green_api_api_token`.
- Глобальные `TELEGRAM_BOT_TOKEN`/`TELEGRAM_WEBHOOK_SECRET` в env —
  hard-fail когда все salons мигрируют на per-business.
- `hmac.compare_digest` для secret-сравнений — отдельный мелкий пункт,
  применить к verify_telegram_request и verify_green_api_request за раз.

---

## ⚠️ Функциональный долг (не утечка данных)

### 2. `get_primary_business` берёт `.first()` membership

**Файл:** `apps/bookings/admin.py:147`

При наличии нескольких `BusinessMembership` у одного user owner-dashboard покажет только первый бизнес. Сейчас не проявляется (по одному салону на юзера), но не масштабируется на multi-business owner.

**Фикс:** либо multi-business агрегация в owner-dashboard, либо явный селектор `?business=<id>`.

---

## 🔧 Архитектурный долг

### 3. `process_incoming_message` — большой state machine (~750 строк)

Декомпозиция `webhooks.py` 2026-05-13 разнесла helpers по 9 модулям, файл сжался 3611 → ~1820 строк. Что осталось — это **core flow**: `process_incoming_message`, `handle_text_message`, `handle_audio_message` + plumbing helpers. Дальнейшая декомпозиция возможна, но `process_incoming_message` — это coherent state machine, дробить её на 5 модулей может ухудшить читаемость. Решать когда вокруг появится новая фича, которая натурально потребует разбиения.

### 4. Cancellation state preemption — узкое место

`CANCEL_CHOOSING` / `CANCEL_CONFIRMING` state handlers стоят **после** `out_of_scope` check и `service_catalog` check в `process_incoming_message`. Это значит, что если клиент в середине cancel-flow задаёт out-of-scope или просит каталог, ответ уходит мимо cancel-state, и при следующем сообщении state всё ещё активен (cancel-сессия "висит" до TTL=60 мин).

Не критично — клиент в худшем случае получит конфузный re-prompt и завершит flow позже. Полный фикс — перенести cancel-handlers выше out_of_scope/service_catalog checks, но это требует перенести также `session = get_or_create_booking_session(...)` выше (сейчас оно сразу после out_of_scope). Сделать когда появится UX feedback от боевых салонов.

---

## ✅ Закрытые

**2026-05-12:**
- `is_affirmative_message` ×3 → ×1 (mojibake-safe версия) — `bc0fa9c`
- N+1 в `get_inbox_dialogs` (180 → 1 запрос) — `5be17ec`
- Opt-out reply локализация (ru/kz) — `773fb10`

**2026-05-13:**
- `security.py` extracted from `webhooks.py` — `183d637`
- `language.py` extracted from `webhooks.py` — `c7a0afe`
- Green API business_id whitelist — partial close of item #1

**2026-05-14 — webhooks.py decomposition (continued):**
- Dead `build_*` v1/v2 overrides removed — `a5c73c0`
- `service_matcher.py` extracted — `b452acc`
- `replies.py` extracted (24 builders) — `eaba3c0`
- `intent.py` + `text_utils.py` extracted — `6651f70`
- `date_parser.py` extracted — `88b32a5`
- `master_matcher.py` extracted — `0976b1a`
- `conversation_context.py` extracted — `9b51c6e`
- Haircut detection moved into `service_matcher.py` — `f6b6e85`
- webhooks.py 3611 → 1769 lines (-51%), 9 focused modules

**2026-05-14 — Cancellation flow (full):**
- `Business.cancellation_policy_hours` field + migration — `7d65af0`
- `cancel_booking_for_client` service helper — `2b81b94`
- 5 cancellation reply builders (no_active / multi / confirmation prompt /
  success / handoff rename) — `68b4056`
- State machine foundations (CANCEL_CHOOSING, CANCEL_CONFIRMING,
  get_client_active_bookings, aborted reply) — `ab4f261`
- State machine wired into `process_incoming_message` with full coverage
  (single / multi / late / race) — `f9f2019`

The bot now self-cancels future bookings when the start is at least
`Business.cancellation_policy_hours` away, asks for confirmation, and
escalates to the operator only when it's too late. No more "promise
without action".

**2026-05-15 — Billing analytics + Reschedule flow:**

_Billing instrumentation + admin dashboard:_
- `prompt_tokens` / `completion_tokens` captured on every AI call,
  including the tool-call follow-up roundtrip — `575b938`
- 30-day cost summary card on `AIInteractionLog` admin changelist
  (totals + per-business breakdown for super_admin) with current
  gpt-4o-mini pricing in tenge — `2157246`

_Reschedule flow (full):_
- Overlap-check + `previous_start_time` in audit payload on the
  `reschedule_appointment` service helper — `abffae2`
- `detect_reschedule_request` keyword detector with explicit
  disjoint check against cancellation keywords — `2969dcc`
- 4 reschedule reply builders (no_active / multi / late_escalation /
  initiated) — `7fa28ee`
- `RESCHEDULE_CHOOSING` state + migration `0017` +
  `build_reschedule_success_reply` — `227c46e`
- Continuation handler + `_route_single_booking_reschedule` routing
  helper (mirrors cancellation pattern) — `3de5ba0`
- Entry block in `process_incoming_message` (IDLE-guarded to avoid
  hijacking mid-flow date-picking phrases like "другой день") +
  `AWAITING_CONFIRMATION` branching that dispatches to
  `reschedule_appointment` when `session.context` carries the
  `reschedule_booking_id` marker — `7d7b50c`
- 6 integration tests covering no-active / late / single-init /
  multi-list / full multi-turn happy path / IDLE-guard regression
  protection — this commit.

The bot now moves bookings to a new slot end-to-end. Same
`Business.cancellation_policy_hours` policy applies (one knob, one
mental model for the owner). Old slot is freed via in-place
`start_time` mutation; new slot is occupied. Audit log carries
`previous_start_time` so the move history is recoverable without
extra Booking fields.

**2026-05-15 — Two-tier reminders:**

- `Booking.day_reminder_sent_at` field + migration `0018` — `a706a56`
- `AIManager.should_send_reminder` and `build_reminder_message` gain a
  `stage` parameter ("hour" / "day") with a tomorrow-notice template
  for the day stage. The day-window is 23..24h before start — a
  booking created less than 24h before start naturally never matches
  the window, so no separate `created_at` check is needed — `590215d`
- `send_booking_reminder` accepts `stage`, uses `message_type="day_reminder"`
  + audit event `day_reminder_queued`. `process_pending_reminders` runs
  a second scan for the day window and queues `.delay(id, stage="day")`.
  `sync_booking_delivery_marker` now also stamps `day_reminder_sent_at`
  on delivery, and `get_outbound_skip_reason` recognises the new
  message_type so a stuck retry never fires a day-reminder at hour
  time — `068443a`
- Restored "За час напомню 😊" in `build_booking_created_reply` and
  `build_reschedule_success_reply`. The promise was removed earlier
  because the day-reminder side was missing; with both stages wired
  up, the bot actually delivers what it advertises. Cancellation
  success deliberately stays without the line — a cancelled booking
  has nothing to remind about — `47d4da6`
- 3 integration tests: scanner queues day-reminder for a 23.5h-ahead
  booking, scanner skips a 5h-ahead booking, full send-then-deliver
  cycle stamps the right field and leaves the other untouched —
  this commit.

The bot's reminder pipeline is now two-tier: day-before notice 23..24h
before start, hour-before reminder ~2h before start, both idempotent
through their own `*_sent_at` fields. Templates per-business override
via `ai_settings["reminder_template"]` / `["day_reminder_template"]`.

---

## Не покрыто ревизией (на будущее)

- `apps/accounts` admin (BusinessMembership / Custom User) — кто может создавать членства
- Celery tasks (`tasks.py`) — корректность `business_id` в background-контексте
- `seed_demo_salon.py` — изоляция в фикстурах
- DRF API (если есть) — отдельный слой безопасности
