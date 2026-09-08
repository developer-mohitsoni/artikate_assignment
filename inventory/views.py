from django.db import IntegrityError, connection, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Asset, CheckOut, Employee
from .serializers import (
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutReturnSerializer,
    CheckOutSerializer,
    EmployeeSummarySerializer,
    OverdueReportRowSerializer,
)


class ConflictError(Exception):
    def __init__(self, detail):
        self.detail = detail


class AssetViewSet(viewsets.ModelViewSet):
    serializer_class = AssetSerializer
    filterset_fields = ["status", "category"]
    search_fields = ["name", "asset_tag"]
    filter_backends = [
        filters.SearchFilter,
    ]

    def get_queryset(self):
        current_checkout = Prefetch(
            "checkouts",
            queryset=CheckOut.objects.filter(returned_at__isnull=True).select_related("employee"),
            to_attr="open_checkouts",
        )
        return Asset.objects.all().prefetch_related(current_checkout).order_by("id")

    def get_serializer(self, *args, **kwargs):
        instance = kwargs.get("instance") or (args[0] if args else None)
        if isinstance(instance, Asset):
            open_checkouts = getattr(instance, "open_checkouts", None)
            if open_checkouts is not None:
                instance.current_checkout = open_checkouts[0] if open_checkouts else None
        return super().get_serializer(*args, **kwargs)

    def filter_queryset(self, queryset):
        status_value = self.request.query_params.get("status")
        category_value = self.request.query_params.get("category")
        if status_value:
            queryset = queryset.filter(status=status_value)
        if category_value:
            queryset = queryset.filter(category=category_value)
        return super().filter_queryset(queryset)


class CheckOutViewSet(viewsets.GenericViewSet):
    queryset = CheckOut.objects.select_related("asset", "employee").all()
    serializer_class = CheckOutSerializer

    def create(self, request):
        serializer = CheckOutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            checkout = self._create_checkout(serializer.validated_data)
        except Asset.DoesNotExist as exc:
            raise NotFound("Unknown asset_tag.") from exc
        except Employee.DoesNotExist as exc:
            raise NotFound("Unknown employee_code.") from exc
        except ConflictError as exc:
            return Response({"detail": exc.detail}, status=status.HTTP_409_CONFLICT)

        return Response(
            CheckOutSerializer(checkout).data,
            status=status.HTTP_201_CREATED,
        )

    @transaction.atomic
    def _create_checkout(self, data):
        asset = Asset.objects.select_for_update().get(asset_tag=data["asset_tag"])
        employee = Employee.objects.select_for_update().get(employee_code=data["employee_code"])

        if not employee.is_active:
            raise ValidationError({"employee_code": "Inactive employees cannot check out assets."})

        if asset.status != Asset.Status.AVAILABLE:
            raise ConflictError("Asset is not available.")

        open_count = CheckOut.objects.filter(
            employee=employee,
            returned_at__isnull=True,
        ).count()
        if open_count >= 3:
            raise ConflictError("Employee already has three open check-outs.")

        updated = Asset.objects.filter(
            pk=asset.pk,
            status=Asset.Status.AVAILABLE,
        ).update(status=Asset.Status.CHECKED_OUT)
        if updated != 1:
            raise ConflictError("Asset is not available.")

        checkout = CheckOut.objects.create(
            asset=asset,
            employee=employee,
            due_at=data["due_at"],
        )
        return CheckOut.objects.select_related("asset", "employee").get(pk=checkout.pk)

    @action(detail=True, methods=["post"], url_path="return")
    def return_asset(self, request, pk=None):
        serializer = CheckOutReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            checkout = self._return_checkout(pk, serializer.validated_data)
        except ConflictError as exc:
            return Response({"detail": exc.detail}, status=status.HTTP_409_CONFLICT)

        return Response(CheckOutSerializer(checkout).data)

    @transaction.atomic
    def _return_checkout(self, pk, data):
        checkout = get_object_or_404(
            CheckOut.objects.select_for_update().select_related("asset", "employee"),
            pk=pk,
        )
        if checkout.returned_at is not None:
            raise ConflictError("Check-out is already returned.")

        checkout.returned_at = timezone.now()
        checkout.condition_note = data.get("condition_note", "")
        checkout.save(update_fields=["returned_at", "condition_note"])

        checkout.asset.status = (
            Asset.Status.MAINTENANCE
            if data.get("needs_maintenance")
            else Asset.Status.AVAILABLE
        )
        checkout.asset.save(update_fields=["status", "updated_at"])
        return checkout


class EmployeeSummaryView(APIView):
    def get(self, request, employee_code):
        now = timezone.now()
        hold_duration = ExpressionWrapper(
            F("checkouts__returned_at") - F("checkouts__checked_out_at"),
            output_field=DurationField(),
        )
        employee = get_object_or_404(
            Employee.objects.filter(employee_code=employee_code).annotate(
                lifetime_checkout_count=Count("checkouts"),
                currently_held_count=Count(
                    "checkouts",
                    filter=Q(checkouts__returned_at__isnull=True),
                ),
                currently_overdue_count=Count(
                    "checkouts",
                    filter=Q(checkouts__returned_at__isnull=True, checkouts__due_at__lt=now),
                ),
                mean_hold_duration=Avg(
                    hold_duration,
                    filter=Q(checkouts__returned_at__isnull=False),
                ),
            )
        )
        mean_days = None
        if employee.mean_hold_duration is not None:
            mean_days = employee.mean_hold_duration.total_seconds() / 86400
        data = {
            "employee_code": employee.employee_code,
            "lifetime_checkout_count": employee.lifetime_checkout_count,
            "currently_held_count": employee.currently_held_count,
            "currently_overdue_count": employee.currently_overdue_count,
            "mean_hold_duration_days": mean_days,
        }
        return Response(EmployeeSummarySerializer(data).data)


class OverdueReportView(APIView):
    def get(self, request):
        now = timezone.now()
        checkouts = (
            CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
            .select_related("asset", "employee")
            .order_by("due_at")
        )
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(checkouts, request, view=self)
        rows = [
            {
                "asset_name": checkout.asset.name,
                "asset_tag": checkout.asset.asset_tag,
                "employee_code": checkout.employee.employee_code,
                "employee_name": checkout.employee.full_name,
                "days_overdue": (now - checkout.due_at).days,
            }
            for checkout in page
        ]
        serializer = OverdueReportRowSerializer(rows, many=True)
        return paginator.get_paginated_response(serializer.data)


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            return Response(
                {"database": "unavailable", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"database": "ok"})
