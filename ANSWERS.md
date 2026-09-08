# Answers

## Part B - Broken Snippets

### Snippet 1 - overdue report view

The main issue is that the view is doing work in Python that should be done by the database. It loads open check-outs first, then checks whether each one is overdue in application code. That is fine with five rows locally, but it gets expensive once the table grows.

There is also an N+1 query problem. If the loop touches `c.asset` and `c.employee` without `select_related`, Django may run extra queries for every row. Sorting in Python has the same problem: it works in a small test database but becomes slow and memory-heavy in production. I would also avoid calling `timezone.now()` inside the loop because each row could be compared against a slightly different timestamp.

A better version pushes filtering and sorting into SQL and fetches the related asset and employee in the same query:

```python
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["GET"])
def overdue_report(request):
    now = timezone.now()
    checkouts = (
        CheckOut.objects
        .filter(returned_at__isnull=True, due_at__lt=now)
        .select_related("asset", "employee")
        .order_by("due_at")
    )

    rows = [
        {
            "asset": c.asset.name,
            "asset_tag": c.asset.asset_tag,
            "employee_code": c.employee.employee_code,
            "employee": c.employee.full_name,
            "days_overdue": (now - c.due_at).days,
        }
        for c in checkouts
    ]
    return Response({"count": len(rows), "rows": rows})
```

The tests I would want here are a query-count test and a test with enough rows to make sure the endpoint is not filtering most of the data in Python.

### Snippet 2 - check-out endpoint

This code has a correctness issue, not just a cleanup issue. Two requests can read the same asset as available and both create a check-out. That is the kind of bug that manual testing usually misses because manual tests are normally single-request.

The endpoint also needs stronger validation. Missing asset or employee records should return `404`, not become an unhandled exception. Inactive employees should be rejected. `due_at` should be validated by a serializer, and the asset update plus check-out creation should happen in a single transaction.

The safer shape is:

```python
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["POST"])
def check_out_asset(request):
    serializer = CheckOutCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    with transaction.atomic():
        asset = get_object_or_404(
            Asset.objects.select_for_update(),
            asset_tag=data["asset_tag"],
        )
        employee = get_object_or_404(Employee, employee_code=data["employee_code"])

        if not employee.is_active:
            return Response({"detail": "inactive employee"}, status=400)
        if asset.status != Asset.Status.AVAILABLE:
            return Response({"detail": "not available"}, status=409)
        if CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3:
            return Response({"detail": "limit reached"}, status=409)

        asset.status = Asset.Status.CHECKED_OUT
        asset.save(update_fields=["status", "updated_at"])
        checkout = CheckOut.objects.create(
            asset=asset,
            employee=employee,
            due_at=data["due_at"],
        )

    return Response({"id": checkout.id}, status=201)
```

The important part is `transaction.atomic()` plus `select_for_update()` on the asset row. I would test this with two concurrent requests for the same asset, plus normal validation tests for inactive employees, missing records, invalid due dates, and the three-open-checkout limit.

### Snippet 3 - nightly notice task

The risky part is that the task creates notices directly. If the task runs twice, or Celery retries after a partial failure, the same overdue check-out can get duplicate notices unless the database prevents it.

For background jobs, I prefer making the database enforce idempotency. In this case that means a unique constraint for one notice per check-out per date, and task code that uses `get_or_create`. I would also pass IDs around instead of model instances, because IDs are safer and cheaper for Celery serialization.

```python
from celery import shared_task
from django.db import IntegrityError
from django.utils import timezone

@shared_task
def send_overdue_notices():
    now = timezone.now()
    today = timezone.localdate()
    created = 0
    overdue_ids = CheckOut.objects.filter(
        returned_at__isnull=True,
        due_at__lt=now,
    ).values_list("id", flat=True)

    for checkout_id in overdue_ids.iterator(chunk_size=1000):
        try:
            notice, was_created = OverdueNotice.objects.get_or_create(
                checkout_id=checkout_id,
                notice_date=today,
            )
        except IntegrityError:
            was_created = False

        if was_created:
            deliver_email.delay(checkout_id)
            created += 1

    return {"created": created}
```

The key test is simple: run the task multiple times on the same day and assert that only one notice exists for the same check-out.

## Part C - PostgreSQL Query Optimisation

The original query is slow mainly because it wraps `checked_out_at` in `DATE(...)`:

```sql
WHERE DATE(c.checked_out_at) BETWEEN '2026-01-01' AND '2026-06-30'
```

That makes it harder for PostgreSQL to use a normal btree index on `checked_out_at`. It also uses `SELECT *`, which usually pulls more columns than a report screen needs.

I would rewrite it as a half-open timestamp range:

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at, c.returned_at
FROM checkouts c
JOIN employees e ON e.id = c.employee_id
WHERE c.checked_out_at >= TIMESTAMPTZ '2026-01-01 00:00:00+00'
  AND c.checked_out_at <  TIMESTAMPTZ '2026-07-01 00:00:00+00'
  AND c.returned_at IS NULL
  AND e.is_active = true
ORDER BY c.due_at ASC;
```

This keeps the indexed column unwrapped, avoids the end-of-day boundary problem, and makes the join path clearer to the planner.

For indexes, I would start with:

```sql
CREATE INDEX CONCURRENTLY idx_checkouts_open_checked_out_due
ON checkouts (checked_out_at, due_at)
WHERE returned_at IS NULL;
```

That index is focused on open check-outs, so it should be much smaller than an index over the full table.

I would only add the employee partial index if the data supports it:

```sql
CREATE INDEX CONCURRENTLY idx_employees_active_id
ON employees (id)
WHERE is_active = true;
```

If almost all employees are active, this second index may not help much. The primary key lookup may already be enough after filtering check-outs.

Before the rewrite, I would expect `EXPLAIN (ANALYZE, BUFFERS)` to show a sequential scan or a large scan on `checkouts`, with many rows removed by the filter and possibly a large sort. After the rewrite, I would expect an index scan or bitmap index scan using the partial checkout index, fewer buffer reads, and fewer rows filtered after scanning.

The main growth risk is the size of the open-checkout working set. If open rows grow a lot, the report and sort will be the first thing to suffer. I would monitor the query plan, paginate reports, archive old returned rows when needed, and only consider partitioning if the data volume and retention rules justify it.

## Part D - Production Reasoning

### D1 - Zero-downtime migration

I would not add `location_id` as a required field in one deploy. On a large table, that can lock the table or break old application instances during rollout.

I would do it in phases:

1. Add `location_id` as nullable.
2. Deploy code that can read and write the new column but still works when it is null.
3. Backfill old rows in small batches outside the request path.
4. Deploy validation that requires `location_id` for new writes.
5. After the backfill is complete and metrics show no new nulls, validate the foreign key and make the column non-null.

The rollback story is also easier this way. During the nullable phase, old code can still run. The dangerous version is a big blocking migration with a non-null default on millions of rows.

### D2 - Latency triage

If the overdue report suddenly becomes slow and there has been no deploy for nine days, I would first check whether this is an app problem or a database problem.

The first checks would be endpoint timing, DB query time, CPU, memory, connection pool usage, and slow query logs. On PostgreSQL I would check `pg_stat_activity`, blocking locks, long transactions, autovacuum, stale stats, table bloat, and `EXPLAIN (ANALYZE, BUFFERS)` for the report query.

Since there was no recent deploy, the most likely causes are data shape or database health. Data shape means many more open overdue check-outs than usual. Database health means things like stale statistics, bloat, lock contention, missing autovacuum progress, or IO pressure.

I would avoid guessing from the application code alone. The useful evidence is row counts by `returned_at` and `due_at`, the actual query plan, and database/host metrics around the time latency increased.

### D3 - CI/CD and safety

For pull requests, I would run formatting/lint checks, unit tests, API tests, migration checks, and a Docker build. Because this assignment depends on transactions and row locks, CI should run against PostgreSQL, not only SQLite.

On merge, I would build an immutable Docker image, run the test suite again, deploy to staging, run smoke tests, and only then deploy to production.

For migrations, I would use the expand-before-contract pattern. Add backward-compatible schema first, deploy code that works with both old and new schema, backfill data, and only later add restrictive constraints or remove old fields.

Rollback depends on the migration phase. If the database was only expanded, rolling back the app is usually safe. If a restrictive migration already ran, I would not blindly roll back to code that cannot handle the new schema; I would roll forward with a fix or use a prepared compatible rollback version.
