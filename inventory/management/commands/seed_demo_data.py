from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework.authtoken.models import Token

from inventory.models import Asset, CheckOut, Employee


class Command(BaseCommand):
    help = "Populate the database with deterministic demo assets, employees, and check-outs."

    def handle(self, *args, **options):
        now = timezone.now()
        demo_asset_tags = [
            "CAM-001",
            "CAM-002",
            "LAP-001",
            "LAP-002",
            "SEN-001",
            "SEN-002",
            "VEH-001",
            "VEH-002",
        ]

        CheckOut.objects.filter(asset__asset_tag__in=demo_asset_tags).delete()

        assets = {
            "CAM-001": ("Sony A7 Field Camera", Asset.Category.CAMERA, Asset.Status.CHECKED_OUT),
            "CAM-002": ("Canon Backup Camera", Asset.Category.CAMERA, Asset.Status.AVAILABLE),
            "LAP-001": ("Dell Latitude Laptop", Asset.Category.LAPTOP, Asset.Status.CHECKED_OUT),
            "LAP-002": ("MacBook Pro Editing Laptop", Asset.Category.LAPTOP, Asset.Status.AVAILABLE),
            "SEN-001": ("Air Quality Sensor", Asset.Category.SENSOR, Asset.Status.CHECKED_OUT),
            "SEN-002": ("Soil Moisture Sensor", Asset.Category.SENSOR, Asset.Status.AVAILABLE),
            "VEH-001": ("Survey Van", Asset.Category.VEHICLE, Asset.Status.AVAILABLE),
            "VEH-002": ("Inspection Bike", Asset.Category.VEHICLE, Asset.Status.AVAILABLE),
        }
        asset_objects = {}
        for tag, (name, category, status) in assets.items():
            asset, _ = Asset.objects.update_or_create(
                asset_tag=tag,
                defaults={
                    "name": name,
                    "category": category,
                    "status": status,
                    "purchase_date": now.date() - timedelta(days=365),
                },
            )
            asset_objects[tag] = asset

        employees = {
            "EMP001": ("Asha Sharma", "asha@example.com", True),
            "EMP002": ("Rohan Mehta", "rohan@example.com", True),
            "EMP003": ("Neha Khan", "neha@example.com", True),
            "EMP004": ("Inactive User", "inactive@example.com", False),
        }
        employee_objects = {}
        for code, (full_name, email, is_active) in employees.items():
            employee, _ = Employee.objects.update_or_create(
                employee_code=code,
                defaults={
                    "full_name": full_name,
                    "email": email,
                    "is_active": is_active,
                },
            )
            employee_objects[code] = employee

        self._create_checkout(
            asset_objects["CAM-001"],
            employee_objects["EMP001"],
            checked_out_at=now - timedelta(days=10),
            due_at=now - timedelta(days=3),
        )
        self._create_checkout(
            asset_objects["LAP-001"],
            employee_objects["EMP002"],
            checked_out_at=now - timedelta(days=6),
            due_at=now - timedelta(days=1),
        )
        self._create_checkout(
            asset_objects["SEN-001"],
            employee_objects["EMP003"],
            checked_out_at=now - timedelta(days=1),
            due_at=now + timedelta(days=7),
        )
        self._create_checkout(
            asset_objects["CAM-002"],
            employee_objects["EMP001"],
            checked_out_at=now - timedelta(days=20),
            due_at=now - timedelta(days=10),
            returned_at=now - timedelta(days=12),
            condition_note="Returned on time.",
        )
        self._create_checkout(
            asset_objects["VEH-001"],
            employee_objects["EMP002"],
            checked_out_at=now - timedelta(days=15),
            due_at=now - timedelta(days=5),
            returned_at=now - timedelta(days=6),
            condition_note="Returned on time.",
        )
        self._create_checkout(
            asset_objects["VEH-002"],
            employee_objects["EMP003"],
            checked_out_at=now - timedelta(days=12),
            due_at=now - timedelta(days=6),
            returned_at=now - timedelta(days=2),
            condition_note="Returned late.",
        )

        user, _ = User.objects.update_or_create(
            username="demo",
            defaults={"email": "demo@example.com", "is_staff": True, "is_superuser": True},
        )
        user.set_password("demo-password")
        user.save()
        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write(self.style.SUCCESS("Seeded demo data."))
        self.stdout.write(f"Demo username: demo")
        self.stdout.write(f"Demo password: demo-password")
        self.stdout.write(f"Demo API token: {token.key}")

    def _create_checkout(self, asset, employee, checked_out_at, due_at, returned_at=None, condition_note=""):
        checkout = CheckOut.objects.create(
            asset=asset,
            employee=employee,
            due_at=due_at,
            returned_at=returned_at,
            condition_note=condition_note,
        )
        CheckOut.objects.filter(pk=checkout.pk).update(checked_out_at=checked_out_at)
        if returned_at is not None:
            Asset.objects.filter(pk=asset.pk).update(status=Asset.Status.AVAILABLE)
        return checkout
