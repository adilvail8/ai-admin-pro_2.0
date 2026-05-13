# Tech Debt

Список технического долга, накопленного по результатам ревизии 2026-05-12.
Обновляется по мере появления / закрытия пунктов.

---

## 🚨 До запуска WhatsApp (безопасность)

### 1. `green_api_webhook` — частичная валидация `business_id` (минимум закрыт)

**Файл:** `apps/bookings/security.py:validate_green_api_business_id` + вызовы в `apps/bookings/views.py`.

**Закрыто в этой сессии:** добавлен whitelist `GREEN_API_BUSINESS_IDS` (env var, default пусто = backward compatible). Применяется в трёх местах:
- `extract_green_api_business_id` (legacy provider payload)
- `process_webhook_request` (legacy internal payload, channel=WHATSAPP)
- `whatsapp_webhook(business_id)` (per-business URL)

Атакующий со знанием `GREEN_API_SHARED_SECRET` больше не может слать webhook с произвольным `business_id` — только с теми, что в whitelist. **На проде обязательно** настроить `GREEN_API_BUSINESS_IDS=2,3` (реальные id Aura/Sultan).

**Остаточный долг:** в рамках whitelist всё ещё возможна подмена между Aura↔Sultan (атакующему нужно знать оба id). Полное закрытие требует per-business credentials:
- Поле `Business.green_api_instance_id` + миграция
- Lookup business по `instanceData.idInstance` из payload вместо доверия `payload["business_id"]`
- Отдельные `apiTokenInstance` на бизнес

Это архитектурное изменение, делается отдельной сессией когда добавится второй реальный Green API instance.

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

---

## Не покрыто ревизией (на будущее)

- `apps/accounts` admin (BusinessMembership / Custom User) — кто может создавать членства
- Celery tasks (`tasks.py`) — корректность `business_id` в background-контексте
- `seed_demo_salon.py` — изоляция в фикстурах
- DRF API (если есть) — отдельный слой безопасности
