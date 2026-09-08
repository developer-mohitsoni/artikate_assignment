from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import OperationalError, close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from inventory.models import Asset, CheckOut, Employee, OverdueNotice
from inventory.tasks import flag_overdue_checkouts


@pytest.fixture
def user(db):
    return User.objects.create_user(username="tester", password="password")


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def create_asset(tag="AST-001", status=Asset.Status.AVAILABLE):
    return Asset.objects.create(
        asset_tag=tag,
        name=f"Asset {tag}",
        category=Asset.Category.CAMERA,
        status=status,
        purchase_date=timezone.localdate() - timedelta(days=30),
    )


def create_employee(code="EMP001", is_active=True):
    return Employee.objects.create(
        employee_code=code,
        full_name=f"Employee {code}",
        email=f"{code.lower()}@example.com",
        is_active=is_active,
    )


def create_checkout(asset, employee, due_at, checked_out_at=None, returned_at=None):
    checkout = CheckOut.objects.create(
        asset=asset,
        employee=employee,
        due_at=due_at,
        returned_at=returned_at,
    )
    if checked_out_at is not None:
        CheckOut.objects.filter(pk=checkout.pk).update(checked_out_at=checked_out_at)
        checkout.refresh_from_db()
    return checkout


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor == "sqlite",
    reason="SQLite table-level locks cannot verify the PostgreSQL row-lock concurrency contract.",
)
def test_two_simultaneous_checkouts_for_one_asset_allow_exactly_one_success(user):
    asset = create_asset()
    employee = create_employee()
    due_at = (timezone.now() + timedelta(days=1)).isoformat()

    def send_request():
        close_old_connections()
        client = APIClient()
        client.force_authenticate(user=user)
        try:
            response = client.post(
                "/api/v1/checkouts/",
                {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code, "due_at": due_at},
                format="json",
            )
        except OperationalError:
            if connection.vendor != "sqlite":
                raise
            close_old_connections()
            return 409
        close_old_connections()
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: send_request(), range(2)))

    assert sorted(statuses) == [201, 409]
    assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT


@pytest.mark.django_db
def test_successful_checkout_creates_row_and_marks_asset_checked_out(api_client):
    asset = create_asset()
    employee = create_employee()

    response = api_client.post(
        "/api/v1/checkouts/",
        {
            "asset_tag": asset.asset_tag,
            "employee_code": employee.employee_code,
            "due_at": (timezone.now() + timedelta(days=5)).isoformat(),
        },
        format="json",
    )

    assert response.status_code == 201
    assert CheckOut.objects.filter(asset=asset, employee=employee, returned_at__isnull=True).count() == 1
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT


@pytest.mark.django_db
def test_unavailable_asset_checkout_returns_conflict(api_client):
    asset = create_asset(status=Asset.Status.MAINTENANCE)
    employee = create_employee()

    response = api_client.post(
        "/api/v1/checkouts/",
        {
            "asset_tag": asset.asset_tag,
            "employee_code": employee.employee_code,
            "due_at": (timezone.now() + timedelta(days=5)).isoformat(),
        },
        format="json",
    )

    assert response.status_code == 409
    assert CheckOut.objects.count() == 0


@pytest.mark.django_db
def test_employee_cannot_create_fourth_open_checkout(api_client):
    employee = create_employee()
    for index in range(3):
        asset = create_asset(tag=f"AST-00{index}")
        create_checkout(asset, employee, timezone.now() + timedelta(days=5))

    fourth_asset = create_asset(tag="AST-004")
    response = api_client.post(
        "/api/v1/checkouts/",
        {
            "asset_tag": fourth_asset.asset_tag,
            "employee_code": employee.employee_code,
            "due_at": (timezone.now() + timedelta(days=5)).isoformat(),
        },
        format="json",
    )

    assert response.status_code == 409
    assert CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() == 3


@pytest.mark.django_db
def test_checkout_due_at_must_be_future_and_within_thirty_days(api_client):
    asset = create_asset()
    employee = create_employee()

    past_due = api_client.post(
        "/api/v1/checkouts/",
        {
            "asset_tag": asset.asset_tag,
            "employee_code": employee.employee_code,
            "due_at": (timezone.now() - timedelta(minutes=1)).isoformat(),
        },
        format="json",
    )
    too_far_due = api_client.post(
        "/api/v1/checkouts/",
        {
            "asset_tag": asset.asset_tag,
            "employee_code": employee.employee_code,
            "due_at": (timezone.now() + timedelta(days=31)).isoformat(),
        },
        format="json",
    )

    assert past_due.status_code == 400
    assert too_far_due.status_code == 400
    assert CheckOut.objects.count() == 0


@pytest.mark.django_db
def test_protected_endpoints_require_authentication():
    client = APIClient()
    get_assets = client.get("/api/v1/assets/")
    get_summary = client.get("/api/v1/employees/EMP001/summary/")
    get_overdue = client.get("/api/v1/reports/overdue/")
    post_checkout = client.post("/api/v1/checkouts/", {}, format="json")

    assert get_assets.status_code == 401
    assert get_summary.status_code == 401
    assert get_overdue.status_code == 401
    assert post_checkout.status_code == 401


@pytest.mark.django_db
def test_health_endpoint_does_not_require_authentication():
    response = APIClient().get("/api/v1/health/")

    assert response.status_code == 200
    assert response.data == {"database": "ok"}


@pytest.mark.django_db
def test_inactive_employee_checkout_returns_bad_request(api_client):
    asset = create_asset()
    employee = create_employee(is_active=False)

    response = api_client.post(
        "/api/v1/checkouts/",
        {
            "asset_tag": asset.asset_tag,
            "employee_code": employee.employee_code,
            "due_at": (timezone.now() + timedelta(days=5)).isoformat(),
        },
        format="json",
    )

    assert response.status_code == 400
    assert CheckOut.objects.count() == 0
    asset.refresh_from_db()
    assert asset.status == Asset.Status.AVAILABLE


@pytest.mark.django_db
def test_unknown_asset_or_employee_returns_not_found(api_client):
    asset = create_asset()
    employee = create_employee()
    due_at = (timezone.now() + timedelta(days=5)).isoformat()

    missing_asset = api_client.post(
        "/api/v1/checkouts/",
        {"asset_tag": "MISSING", "employee_code": employee.employee_code, "due_at": due_at},
        format="json",
    )
    missing_employee = api_client.post(
        "/api/v1/checkouts/",
        {"asset_tag": asset.asset_tag, "employee_code": "MISSING", "due_at": due_at},
        format="json",
    )

    assert missing_asset.status_code == 404
    assert missing_employee.status_code == 404


@pytest.mark.django_db
def test_return_endpoint_can_mark_asset_for_maintenance(api_client):
    asset = create_asset(status=Asset.Status.CHECKED_OUT)
    employee = create_employee()
    checkout = create_checkout(asset, employee, timezone.now() + timedelta(days=5))

    response = api_client.post(
        f"/api/v1/checkouts/{checkout.pk}/return/",
        {"condition_note": "Lens cracked.", "needs_maintenance": True},
        format="json",
    )

    assert response.status_code == 200
    checkout.refresh_from_db()
    asset.refresh_from_db()
    assert checkout.returned_at is not None
    assert checkout.condition_note == "Lens cracked."
    assert asset.status == Asset.Status.MAINTENANCE


@pytest.mark.django_db
def test_returning_already_returned_checkout_returns_conflict(api_client):
    asset = create_asset(status=Asset.Status.AVAILABLE)
    employee = create_employee()
    checkout = create_checkout(
        asset,
        employee,
        timezone.now() + timedelta(days=5),
        returned_at=timezone.now(),
    )

    response = api_client.post(
        f"/api/v1/checkouts/{checkout.pk}/return/",
        {"condition_note": "", "needs_maintenance": False},
        format="json",
    )

    assert response.status_code == 409


@pytest.mark.django_db
def test_checkout_rolls_back_asset_status_when_checkout_create_fails(api_client):
    asset = create_asset()
    employee = create_employee()

    with patch("inventory.views.CheckOut.objects.create", side_effect=RuntimeError("forced failure")):
        with pytest.raises(RuntimeError, match="forced failure"):
            api_client.post(
                "/api/v1/checkouts/",
                {
                    "asset_tag": asset.asset_tag,
                    "employee_code": employee.employee_code,
                    "due_at": (timezone.now() + timedelta(days=5)).isoformat(),
                },
                format="json",
            )

    asset.refresh_from_db()
    assert asset.status == Asset.Status.AVAILABLE
    assert CheckOut.objects.count() == 0


@pytest.mark.django_db
def test_assets_create_list_filters_search_and_retrieve_current_holder(api_client):
    camera = create_asset(tag="CAM-FILTER")
    camera.name = "Alpha Camera Kit"
    camera.save(update_fields=["name"])
    laptop = Asset.objects.create(
        asset_tag="LAP-FILTER",
        name="Beta Laptop",
        category=Asset.Category.LAPTOP,
        status=Asset.Status.AVAILABLE,
        purchase_date=timezone.localdate(),
    )
    employee = create_employee()
    create_checkout(camera, employee, timezone.now() + timedelta(days=5))
    camera.status = Asset.Status.CHECKED_OUT
    camera.save(update_fields=["status"])

    create_response = api_client.post(
        "/api/v1/assets/",
        {
            "asset_tag": "SEN-CREATE",
            "name": "Created Sensor",
            "category": Asset.Category.SENSOR,
            "status": Asset.Status.AVAILABLE,
            "purchase_date": str(timezone.localdate()),
        },
        format="json",
    )
    filtered = api_client.get("/api/v1/assets/?status=AVAILABLE&category=LAPTOP")
    searched = api_client.get("/api/v1/assets/?search=Alpha")
    retrieved = api_client.get(f"/api/v1/assets/{camera.pk}/")

    assert create_response.status_code == 201
    assert filtered.status_code == 200
    assert [row["asset_tag"] for row in filtered.data["results"]] == [laptop.asset_tag]
    assert searched.status_code == 200
    assert searched.data["results"][0]["asset_tag"] == camera.asset_tag
    assert retrieved.status_code == 200
    assert retrieved.data["current_holder"] == {
        "employee_code": employee.employee_code,
        "full_name": employee.full_name,
    }


@pytest.mark.django_db
def test_asset_list_is_paginated_at_twenty_items(api_client):
    for index in range(21):
        create_asset(tag=f"AST-LIST-{index:02d}")

    response = api_client.get("/api/v1/assets/")

    assert response.status_code == 200
    assert response.data["count"] == 21
    assert response.data["next"] is not None
    assert len(response.data["results"]) == 20


@pytest.mark.django_db
def test_overdue_report_excludes_item_due_exactly_now(api_client):
    fixed_now = timezone.now()
    employee = create_employee()
    overdue_asset = create_asset(tag="AST-OVERDUE")
    exact_asset = create_asset(tag="AST-EXACT")
    create_checkout(overdue_asset, employee, fixed_now - timedelta(days=2))
    create_checkout(exact_asset, employee, fixed_now)

    with patch("inventory.views.timezone.now", return_value=fixed_now):
        response = api_client.get("/api/v1/reports/overdue/")

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["asset_tag"] == "AST-OVERDUE"
    assert response.data["results"][0]["days_overdue"] == 2


@pytest.mark.django_db
def test_overdue_report_does_not_issue_query_per_row(api_client):
    employee = create_employee()
    now = timezone.now()
    for index in range(3):
        asset = create_asset(tag=f"AST-N1-{index}")
        create_checkout(asset, employee, now - timedelta(days=index + 1))

    with CaptureQueriesContext(connection) as captured:
        response = api_client.get("/api/v1/reports/overdue/")

    assert response.status_code == 200
    assert response.data["count"] == 3
    assert len(response.data["results"]) == 3
    assert len(captured) <= 3


@pytest.mark.django_db
def test_overdue_report_is_paginated_at_twenty_items(api_client):
    employee = create_employee()
    now = timezone.now()
    for index in range(21):
        asset = create_asset(tag=f"AST-PAGE-{index:02d}")
        create_checkout(asset, employee, now - timedelta(days=index + 1))

    response = api_client.get("/api/v1/reports/overdue/")

    assert response.status_code == 200
    assert response.data["count"] == 21
    assert response.data["next"] is not None
    assert len(response.data["results"]) == 20


@pytest.mark.django_db
def test_employee_summary_returns_database_aggregates(api_client):
    employee = create_employee()
    other_employee = create_employee(code="EMP002")
    now = timezone.now()

    returned_fast_asset = create_asset(tag="AST-FAST")
    returned_slow_asset = create_asset(tag="AST-SLOW")
    open_asset = create_asset(tag="AST-OPEN")
    overdue_asset = create_asset(tag="AST-LATE")
    other_asset = create_asset(tag="AST-OTHER")

    create_checkout(
        returned_fast_asset,
        employee,
        due_at=now - timedelta(days=4),
        checked_out_at=now - timedelta(days=5),
        returned_at=now - timedelta(days=3),
    )
    create_checkout(
        returned_slow_asset,
        employee,
        due_at=now - timedelta(days=7),
        checked_out_at=now - timedelta(days=10),
        returned_at=now - timedelta(days=4),
    )
    create_checkout(open_asset, employee, due_at=now + timedelta(days=2))
    create_checkout(overdue_asset, employee, due_at=now - timedelta(days=1))
    create_checkout(other_asset, other_employee, due_at=now - timedelta(days=1))

    response = api_client.get(f"/api/v1/employees/{employee.employee_code}/summary/")

    assert response.status_code == 200
    assert response.data["lifetime_checkout_count"] == 4
    assert response.data["currently_held_count"] == 2
    assert response.data["currently_overdue_count"] == 1
    assert response.data["mean_hold_duration_days"] == pytest.approx(4.0)


@pytest.mark.django_db
def test_employee_summary_handles_no_returned_items(api_client):
    employee = create_employee()
    asset = create_asset()
    create_checkout(asset, employee, timezone.now() + timedelta(days=2))

    response = api_client.get(f"/api/v1/employees/{employee.employee_code}/summary/")

    assert response.status_code == 200
    assert response.data["lifetime_checkout_count"] == 1
    assert response.data["currently_held_count"] == 1
    assert response.data["currently_overdue_count"] == 0
    assert response.data["mean_hold_duration_days"] is None


@pytest.mark.django_db
def test_flag_overdue_checkouts_is_idempotent():
    employee = create_employee()
    asset = create_asset()
    checkout = create_checkout(asset, employee, timezone.now() - timedelta(days=1))

    first = flag_overdue_checkouts()
    second = flag_overdue_checkouts()

    assert first == {"created": 1}
    assert second == {"created": 0}
    assert OverdueNotice.objects.filter(checkout=checkout, notice_date=timezone.localdate()).count() == 1
