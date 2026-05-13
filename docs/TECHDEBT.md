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

### 3. `apps/bookings/webhooks.py` — 3644 строки в одном файле

Сложно поддерживать, легко словить циклические импорты при правке. План декомпозиции:

- `webhooks/normalizers.py` — Telegram / Green API payload parsing
- `webhooks/router.py` — `process_incoming_message`, диспетчер
- `webhooks/replies.py` — все `build_*_reply` функции
- `webhooks/security.py` — `verify_*_token`, rate limiting
- `webhooks/language.py` — `detect_client_language`, локализация
- `webhooks/views.py` — Django views (тонкие обёртки)

Делать осторожно, pytest после каждого выноса.

---

## 📝 Мёртвый код (низкий приоритет)

### 4. `build_*` v1-блок в `webhooks.py:1190-1301`

4 функции (`build_master_recommendation_reply`, `build_service_master_options_reply`, `build_service_catalog_reply`, `build_master_list_reply`) имеют по второму определению в конце файла (overrides), которое побеждает в namespace. Версии в конце — сознательное продуктовое решение "compact and human style", защищённое тестами `test_build_*_is_compact_and_human_for_russian`.

Первые определения никогда не вызываются.

**Условие удаления:** после полной карты дублей в файле — есть и другие функции (`build_service_price_reply`, `build_price_clarification_reply`, `build_working_hours_reply`, `build_booking_confirmation_reply`, `build_booking_created_reply` x3, `build_existing_booking_reply` x3, `build_date_selection_reply`), у которых тоже могут быть override-варианты. Удалять только после понимания всех пар.

Связано с пунктом 3 — естественно решается при декомпозиции.

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

---

## Не покрыто ревизией (на будущее)

- `apps/accounts` admin (BusinessMembership / Custom User) — кто может создавать членства
- Celery tasks (`tasks.py`) — корректность `business_id` в background-контексте
- `seed_demo_salon.py` — изоляция в фикстурах
- DRF API (если есть) — отдельный слой безопасности
