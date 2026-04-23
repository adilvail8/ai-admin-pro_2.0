from apps.accounts.models import BusinessMembership
from rest_framework.permissions import BasePermission


ROLE_HIERARCHY = {
    BusinessMembership.Role.STAFF: 0,
    BusinessMembership.Role.ADMIN: 1,
    BusinessMembership.Role.OWNER: 2,
}


def _roles_gte(min_role: str) -> list[str]:
    try:
        min_level = ROLE_HIERARCHY[min_role]
    except KeyError as error:
        raise ValueError(f"Unknown business role: {min_role}") from error

    return [
        role
        for role, level in ROLE_HIERARCHY.items()
        if level >= min_level
    ]


def BusinessAccessPermission(min_role: str = BusinessMembership.Role.STAFF):
    allowed_roles = tuple(_roles_gte(min_role))

    class _BusinessAccessPermission(BasePermission):
        message = "You do not have access to this business."

        def has_permission(self, request, view):
            user = getattr(request, "user", None)
            business = getattr(view, "business", None)

            if not user or not user.is_authenticated:
                return False
            if business is None:
                return False

            return BusinessMembership.objects.filter(
                user=user,
                business=business,
                is_active=True,
                role__in=allowed_roles,
            ).exists()

        def has_object_permission(self, request, view, obj):
            business = getattr(view, "business", None)
            if business is None:
                return False

            if hasattr(view, "get_object_business_id"):
                object_business_id = view.get_object_business_id(obj)
            else:
                object_business_id = getattr(obj, "business_id", None)

            return object_business_id == business.id

    permission_name = f"BusinessAccessPermission_{min_role}"
    _BusinessAccessPermission.__name__ = permission_name
    _BusinessAccessPermission.__qualname__ = permission_name
    return _BusinessAccessPermission

