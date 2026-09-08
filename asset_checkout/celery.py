import os

from celery import Celery


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "asset_checkout.settings")

app = Celery("asset_checkout")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
