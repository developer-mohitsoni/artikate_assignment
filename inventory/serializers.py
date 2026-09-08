from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import Asset, CheckOut, Employee


class AssetSerializer(serializers.ModelSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = [
            "id",
            "asset_tag",
            "name",
            "category",
            "status",
            "purchase_date",
            "created_at",
            "updated_at",
            "current_holder",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "current_holder"]

    def get_current_holder(self, obj):
        checkout = getattr(obj, "current_checkout", None)
        open_checkouts = getattr(obj, "open_checkouts", None)
        if checkout is None and open_checkouts is not None:
            checkout = open_checkouts[0] if open_checkouts else None
        if checkout is None:
            checkout = (
                obj.checkouts.filter(returned_at__isnull=True)
                .select_related("employee")
                .first()
            )
        if checkout is None:
            return None
        return {
            "employee_code": checkout.employee.employee_code,
            "full_name": checkout.employee.full_name,
        }


class CheckOutSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = CheckOut
        fields = [
            "id",
            "asset",
            "asset_tag",
            "employee",
            "employee_code",
            "checked_out_at",
            "due_at",
            "returned_at",
            "condition_note",
        ]
        read_only_fields = [
            "id",
            "asset",
            "asset_tag",
            "employee",
            "employee_code",
            "checked_out_at",
            "returned_at",
            "condition_note",
        ]


class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError("due_at must be in the future.")
        if value > now + timedelta(days=30):
            raise serializers.ValidationError("due_at cannot be more than 30 days ahead.")
        return value


class CheckOutReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(required=False, allow_blank=True)
    needs_maintenance = serializers.BooleanField(default=False)


class OverdueReportRowSerializer(serializers.Serializer):
    asset_name = serializers.CharField()
    asset_tag = serializers.CharField()
    employee_code = serializers.CharField()
    employee_name = serializers.CharField()
    days_overdue = serializers.IntegerField()


class EmployeeSummarySerializer(serializers.Serializer):
    employee_code = serializers.CharField()
    lifetime_checkout_count = serializers.IntegerField()
    currently_held_count = serializers.IntegerField()
    currently_overdue_count = serializers.IntegerField()
    mean_hold_duration_days = serializers.FloatField(allow_null=True)
