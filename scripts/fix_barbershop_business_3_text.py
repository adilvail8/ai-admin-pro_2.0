from apps.bookings.models import Business, Master


MASTER_FIXTURES = {
    4: {
        "full_name": "Нурсултан Абиров",
        "specialization": "Barber-colorist",
    },
    5: {
        "full_name": "Азамат Сагын",
        "specialization": "Barber",
    },
    6: {
        "full_name": "Руслан Толеу",
        "specialization": "Barber",
    },
}


def main():
    business = Business.objects.get(pk=3)

    business.name = "Sultan Barbershop"
    business.brand_name = "Sultan Barbershop"
    business.address = "Абая 150/230"
    business.city = "Кызылорда"
    business.working_hours = "Mon-Thu 10:00-20:00, Fri-Sat 10:00-21:00, Sun 11:00-19:00"
    business.timezone_name = "Asia/Qyzylorda"
    business.knowledge_base = (
        "Это мужской барбершоп. Основной фокус — мужские стрижки, фейд, борода, "
        "детские стрижки для мальчиков и окрашивание волос. "
        "Бот не должен предлагать женские услуги, ресницы, брови, маникюр или педикюр."
    )
    business.ai_settings = {
        "tone": "Calm & Direct",
        "temperature": 0.1,
    }

    existing_rules = business.ai_rules if isinstance(business.ai_rules, dict) else {}
    existing_rules["rules"] = [
        "Отвечай как администратор барбершопа: коротко и по делу.",
        "Не предлагай женские услуги, ресницы, брови, маникюр или педикюр.",
        "Если клиент просит небарберскую услугу, честно скажи, что в барбершопе её нет.",
    ]
    business.ai_rules = existing_rules
    business.save()

    for master_id, payload in MASTER_FIXTURES.items():
        Master.objects.filter(pk=master_id, business=business).update(**payload)

    print(
        {
            "business_id": business.id,
            "name": business.name,
            "city": business.city,
            "masters_fixed": sorted(MASTER_FIXTURES),
        }
    )


main()
