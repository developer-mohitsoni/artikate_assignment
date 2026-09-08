from celery import shared_task
from django.db import IntegrityError
from django.utils import timezone

from .models import CheckOut, OverdueNotice


@shared_task
def flag_overdue_checkouts():
    today = timezone.localdate()
    now = timezone.now()
    created = 0
    overdue_ids = CheckOut.objects.filter(
        returned_at__isnull=True,
        due_at__lt=now,
    ).values_list("id", flat=True)

    for checkout_id in overdue_ids.iterator():
        try:
            _, was_created = OverdueNotice.objects.get_or_create(
                checkout_id=checkout_id,
                notice_date=today,
            )
        except IntegrityError:
            was_created = False
        if was_created:
            created += 1
    return {"created": created}
