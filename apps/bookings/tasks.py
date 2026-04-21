from celery import shared_task


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True)
def sync_business_with_altegio(self, business_id: int):
    return {"business_id": business_id, "status": "scheduled"}

