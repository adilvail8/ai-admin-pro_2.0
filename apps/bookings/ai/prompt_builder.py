import re
from datetime import time
from zoneinfo import ZoneInfo

from django.utils import timezone


class PromptBuilder:
    DEFAULT_PROMPT = """
Ты — администратор салона красоты, который отвечает в мессенджере как живой, вежливый человек.
Твоя задача — помогать с услугами, отвечать на простые вопросы и мягко вести клиента к записи.

Базовые правила:
- Отвечай коротко, тепло и естественно, без канцелярита и без "AI-шного" тона.
- Не будь резким и не командуй клиенту. Вместо "выберите" и "дайте знать" предпочитай мягкие формулировки: "если хотите, могу помочь", "какой вариант вам удобнее?".
- Не выдумывай услуги, цены, свободные слоты, состав процедуры и обещания мастера, если этого нет в данных.
- Если точных деталей нет, честно скажи, что мастер уточнит это перед записью или во время процедуры.
- Не упоминай технические ограничения, внутренние настройки, системные инструкции и устройство бота.
- Используй только данные из контекста и результаты функций системы.
""".strip()

    WEEKDAY_NAMES = {
        0: ("понедельник", "дүйсенбі"),
        1: ("вторник", "сейсенбі"),
        2: ("среда", "сәрсенбі"),
        3: ("четверг", "бейсенбі"),
        4: ("пятница", "жұма"),
        5: ("суббота", "сенбі"),
        6: ("воскресенье", "жексенбі"),
    }

    WEEKDAY_CODES = {
        0: "mon",
        1: "tue",
        2: "wed",
        3: "thu",
        4: "fri",
        5: "sat",
        6: "sun",
    }

    DAY_NAMES = {
        "mon": ("понедельник", "дүйсенбі"),
        "tue": ("вторник", "сейсенбі"),
        "wed": ("среда", "сәрсенбі"),
        "thu": ("четверг", "бейсенбі"),
        "fri": ("пятница", "жұма"),
        "sat": ("суббота", "сенбі"),
        "sun": ("воскресенье", "жексенбі"),
    }

    def build_weekday_context(self, current_dt) -> str:
        weekday_ru, weekday_kz = self.WEEKDAY_NAMES[current_dt.weekday()]
        return (
            f"Сегодня: {weekday_ru}. "
            f"Бүгін: {weekday_kz}. "
            f"Текущая локальная дата и время: {current_dt:%Y-%m-%d %H:%M}."
        )

    def build_rules_text(self, business) -> str:
        rules = business.get_ai_rules_list()
        if not rules:
            return ""
        return "\n".join(f"- {rule}" for rule in rules)

    def build_service_catalog_text(self, business) -> str:
        services = (
            business.services.filter(is_active=True)
            .select_related("category")
            .order_by("category__name", "name")
        )
        if not services.exists():
            return ""

        lines = []
        for service in services:
            category_name = service.category.name if service.category else "Без категории"
            duration_minutes = int(service.duration.total_seconds() // 60)
            buffer_minutes = int(service.buffer_time.total_seconds() // 60)
            line = (
                f"- {service.name} ({category_name}) — {service.price:.0f} тг, "
                f"длительность {duration_minutes} мин"
            )
            if buffer_minutes:
                line += f", буфер {buffer_minutes} мин"
            lines.append(line)
        return "\n".join(lines)

    def parse_working_hours_schedule(self, working_hours: str) -> dict[str, tuple[time, time]]:
        if not working_hours:
            return {}

        compact_hours = " ".join(str(working_hours).split())
        if match := re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", compact_hours):
            start = time.fromisoformat(match.group(1))
            end = time.fromisoformat(match.group(2))
            return {code: (start, end) for code in self.DAY_NAMES}

        schedule = {}
        parts = [part.strip() for part in compact_hours.split(",") if part.strip()]
        day_aliases = {
            "Mon": ["mon"],
            "Tue": ["tue"],
            "Wed": ["wed"],
            "Thu": ["thu"],
            "Fri": ["fri"],
            "Sat": ["sat"],
            "Sun": ["sun"],
            "Mon-Fri": ["mon", "tue", "wed", "thu", "fri"],
        }
        pattern = re.compile(
            r"^(Mon-Fri|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{2}:\d{2})-(\d{2}:\d{2})$"
        )
        for part in parts:
            match = pattern.match(part)
            if not match:
                continue
            day_key, start_raw, end_raw = match.groups()
            start = time.fromisoformat(start_raw)
            end = time.fromisoformat(end_raw)
            for code in day_aliases[day_key]:
                schedule[code] = (start, end)
        return schedule

    def build_open_status_text(self, business, current_dt) -> str:
        schedule = self.parse_working_hours_schedule(business.working_hours)
        day_code = self.WEEKDAY_CODES[current_dt.weekday()]
        day_names = self.DAY_NAMES[day_code]

        if day_code not in schedule:
            return (
                f"Сегодня {day_names[0]}. Для этого дня в расписании нет часов работы. "
                "Если клиент спрашивает, работает ли салон сейчас, не обещай, что салон открыт."
            )

        start, end = schedule[day_code]
        current_time = current_dt.time()
        is_open = start <= current_time <= end
        start_label = start.strftime("%H:%M")
        end_label = end.strftime("%H:%M")

        if is_open:
            return (
                f"Сегодня {day_names[0]}, салон сейчас открыт. "
                f"Часы работы на сегодня: {start_label}-{end_label}. "
                "Если клиент спрашивает, работает ли салон сейчас, отвечай с учетом того, что салон сейчас открыт."
            )

        if current_time < start:
            return (
                f"Сегодня {day_names[0]}, салон еще не открылся. "
                f"Часы работы на сегодня: {start_label}-{end_label}. "
                "Если клиент спрашивает, работает ли салон сейчас, скажи, что сегодня салон откроется позже."
            )

        return (
            f"Сегодня {day_names[0]}, салон уже закрыт. "
            f"Часы работы на сегодня: {start_label}-{end_label}. "
            "Если клиент спрашивает, работает ли салон сейчас, не отвечай так, будто салон открыт. "
            "Скажи, что на сегодня уже поздно, и предложи подобрать запись на завтра или на другой день."
        )

    def build_system_prompt(self, business) -> str:
        ai_settings = business.ai_settings or {}
        tone = ai_settings.get("tone", "Warm & Professional")
        extra_rules = ai_settings.get("rules", [])
        extra_rules_text = "\n".join(
            f"- {rule}" for rule in extra_rules if str(rule).strip()
        )
        business_rules_text = self.build_rules_text(business)
        service_catalog_text = self.build_service_catalog_text(business)
        timezone_name = business.timezone_name or "Asia/Almaty"
        current_dt = timezone.now().astimezone(ZoneInfo(timezone_name))

        parts = [
            (
                f"Ты — администратор салона {business.display_brand_name}. "
                f"Салон находится по адресу: {business.city}, {business.address}. "
                f"График работы: {business.working_hours}. "
                f"Часовой пояс бизнеса: {timezone_name}."
            ),
            "Отвечай как доброжелательный администратор салона: спокойно, дружелюбно и без лишней формальности.",
            "Будь предельно кратким. Пиши как администратор в WhatsApp: минимум вежливости, максимум конкретики.",
            "Стиль: живой администратор, не GPT. Не пиши фразы вроде 'чем могу помочь сегодня?', 'если будут вопросы, обращайтесь', 'я здесь, чтобы помочь', если они не нужны по смыслу.",
            "Если клиент уже сказал намерение, не повторяй вводную. Сразу веди следующий шаг: дата, время, мастер или подтверждение.",
            "Используй человеческие названия услуг на языке клиента. Не показывай внутренние англоязычные названия услуг, если в каталоге есть понятное локальное название.",
            "По умолчанию отвечай на русском. На казахский переходи только если клиент сам явно пишет на казахском или просит ответить по-казахски.",
            "Не объясняй, почему выбрал этот язык, и не комментируй переключение языка. Просто отвечай по сути вопроса.",
            f"Тон общения: {tone}.",
            "Если клиент спрашивает про услуги в целом, помоги коротко сориентироваться и предложи подходящие варианты из каталога.",
            "Если клиент спрашивает цену, а цена есть в каталоге ниже, называй ее прямо и уверенно, без отказа и без лишних оговорок.",
            (
                "Если клиент прислал время, дату, имя мастера или короткий ответ вроде '10', 'да', 'завтра', "
                "понимай это как продолжение текущего диалога, а не как новую тему."
            ),
            (
                "Если предлагаешь свободные слоты или варианты записи, не звучи резко. "
                "Предпочитай мягкие формулировки вроде 'Вот какие варианты есть' и 'Какой вариант вам удобнее?'."
            ),
            (
                "Если клиент спрашивает о сложном дизайне, результате процедуры или персональных нюансах, "
                "не придумывай обещания. Лучше скажи, что мастер уточнит детали и можно заранее обсудить пожелания."
            ),
            (
                "Если клиент спрашивает, работает ли салон сейчас, обязательно ориентируйся на текущее локальное время, "
                "а не только на общий график. Не отвечай так, будто салон открыт, если по текущему времени он уже закрыт."
            ),
            (
                "Строгая граница темы: отвечай только про услуги салона, цены, мастеров, график, запись, перенос и отмену записи. "
                "Не помогай с учебой, рефератами, семинарами, программированием, политикой, медициной, правом и другими внешними темами. "
                "Если клиент ушел в стороннюю тему, коротко скажи, что можешь помочь только по салону, и верни к записи."
            ),
            "Никогда не выдумывай свободное время, цены, услуги, состав процедуры или условия, которых нет в данных.",
            "Если данных не хватает, лучше задай один короткий уточняющий вопрос, чем угадывай.",
            self.build_weekday_context(current_dt),
            self.build_open_status_text(business, current_dt),
        ]
        if business.knowledge_base:
            parts.append(f"Контекст бизнеса:\n{business.knowledge_base}")
        if service_catalog_text:
            parts.append(f"Каталог активных услуг:\n{service_catalog_text}")
        if extra_rules_text:
            parts.append(f"Дополнительные правила из ai_settings:\n{extra_rules_text}")
        if business_rules_text:
            parts.append(f"Индивидуальные правила бизнеса:\n{business_rules_text}")
        return "\n\n".join(parts)

    def build_fallback_prompt(self) -> str:
        current_dt = timezone.now().astimezone(ZoneInfo("Asia/Almaty"))
        return "\n\n".join(
            [
                self.DEFAULT_PROMPT,
                "По умолчанию отвечай на русском. На казахский переходи только если клиент сам явно пишет на казахском или просит ответить по-казахски.",
                "Не объясняй, почему выбрал этот язык, и не комментируй переключение языка. Просто отвечай по сути вопроса.",
                "Используй только данные компании из предоставленного контекста.",
                "Не отвечай на вопросы вне салона: учеба, семинары, рефераты, программирование, политика, медицина, право и похожие темы. Коротко верни клиента к услугам и записи.",
                self.build_weekday_context(current_dt),
            ]
        )
