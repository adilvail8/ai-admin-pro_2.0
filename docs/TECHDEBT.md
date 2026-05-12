# Tech Debt

Список технического долга, накопленного по результатам ревизии 2026-05-12.
Обновляется по мере появления / закрытия пунктов.

---

## 🚨 До запуска WhatsApp (безопасность)

### 1. `green_api_webhook` — отсутствует валидация `business_id`

**Файл:** `apps/bookings/views.py:140` (`extract_green_api_business_id`)

`business_id` принимается из payload или GET-параметра без проверки соответствия `instanceData.idInstance`. Один глобальный `GREEN_API_SHARED_SECRET` на все салоны → знающий секрет может писать webhook-сообщения от имени любого салона.

**Варианты фикса (по возрастанию строгости):**
1. Production-минимум: настроить `GREEN_API_ALLOWED_IPS` на проде (только официальные Green API IPs).
2. Deprecate legacy `green_api_webhook` в пользу per-business `whatsapp_webhook(business_id)` (URL: `/whatsapp/<business_id>`).
3. Per-business `GREEN_API_INSTANCE_ID` в `Business` модели + lookup `idInstance → business` в `extract_green_api_business_id`.

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

## ✅ Закрытые в этой сессии (2026-05-12)

- `is_affirmative_message` ×3 → ×1 (mojibake-safe версия) — `bc0fa9c`
- N+1 в `get_inbox_dialogs` (180 → 1 запрос) — `5be17ec`
- Opt-out reply локализация (ru/kz) — `773fb10`

---

## Не покрыто ревизией (на будущее)

- `apps/accounts` admin (BusinessMembership / Custom User) — кто может создавать членства
- Celery tasks (`tasks.py`) — корректность `business_id` в background-контексте
- `seed_demo_salon.py` — изоляция в фикстурах
- DRF API (если есть) — отдельный слой безопасности
