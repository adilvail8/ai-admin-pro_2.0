from .models import AuditLog


def create_audit_log(
    *,
    business,
    event_type: str,
    actor_type: str = AuditLog.ActorType.SYSTEM,
    client=None,
    booking=None,
    outbound_message=None,
    channel: str = "",
    payload: dict | None = None,
):
    return AuditLog.objects.create(
        business=business,
        client=client,
        booking=booking,
        outbound_message=outbound_message,
        actor_type=actor_type,
        event_type=event_type,
        channel=channel,
        payload=payload or {},
    )
