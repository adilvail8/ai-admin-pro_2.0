from django.http import Http404
from django.shortcuts import get_object_or_404

from apps.bookings.models import Business


class BusinessContextMixin:
    business_lookup_kwarg = "business_id"
    business = None

    def get_business_queryset(self):
        return Business.objects.filter(is_active=True)

    def get_business_lookup_value(self):
        business_id = self.kwargs.get(self.business_lookup_kwarg)
        if business_id is None:
            raise Http404("Business scope is required.")
        return business_id

    def resolve_business(self):
        return get_object_or_404(
            self.get_business_queryset(),
            pk=self.get_business_lookup_value(),
        )

    def initial(self, request, *args, **kwargs):
        if not request.user or not request.user.is_authenticated:
            super().initial(request, *args, **kwargs)
            return

        self.business = self.resolve_business()
        super().initial(request, *args, **kwargs)


class BusinessScopedQuerysetMixin(BusinessContextMixin):
    business_filter_field = "business"
    queryset = None

    def get_base_queryset(self):
        assert self.queryset is not None, (
            f"'{self.__class__.__name__}' should either define `queryset` "
            "or override `get_base_queryset()`."
        )
        return self.queryset.all()

    def scope_queryset_to_business(self, queryset):
        return queryset.filter(
            **{self.business_filter_field: self.business}
        )

    def get_queryset(self):
        return self.scope_queryset_to_business(self.get_base_queryset())

    def get_object_business_id(self, obj):
        return getattr(obj, "business_id", None)
