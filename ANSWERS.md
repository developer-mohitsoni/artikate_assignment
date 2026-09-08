# Answers

## Part B - Diagnose Broken Snippets

### Snippet 1 - overdue report view

What is wrong:

- It loads all open check-outs and filters overdue rows in Python instead of the database.
- It causes N+1 queries by accessing `c.asset` and `c.employee` inside the loop.
- It calls `timezone.now()` repeatedly, so rows can be evaluated against slightly different times.
- Sorting happens in Python instead of SQL.
- It omits employee code even though the report requirement needs it.

Why local testing hides it:

- Small local datasets do not show memory, sorting, or N+1 query cost.
- With only a few rows, repeated `timezone.now()` calls rarely produce visible differences.
- Local tests often check only response shape, not query count.

Fix:

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

What would catch it:

- Query count tests.
- Tests with many open non-overdue check-outs.
- Code review focused on ORM filtering and `select_related`.

### Snippet 2 - check-out endpoint

What is wrong:

- `Asset.objects.get()` and `Employee.objects.get()` can raise 500 instead of returning 404.
- It does not reject inactive employees.
- It does not validate `due_at`.
- It is not atomic: checkout creation can succeed while asset status update fails.
- It has a race condition. Two requests can both see the asset as available.
- It checks the employee open-count without locking or enforcing a database-backed transition.
- It stores `request.data["due_at"]` without serializer validation.

Why local testing hides it:

- Single-user manual testing is not concurrent.
- Happy-path data always includes valid asset and employee codes.
- Local tests usually do not simulate database failure between two writes.

Fix:

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

What would catch it:

- Concurrency test with two simultaneous requests for one asset.
- Tests for inactive employee, unknown asset, unknown employee, and invalid `due_at`.
- Transaction-focused code review.

### Snippet 3 - nightly notice task

What is wrong:

- It blindly creates notices, so retries or repeated runs create duplicates unless the DB rejects them.
- If email delivery fails after notice creation, retry behavior can create inconsistent duplicate side effects.
- It passes model instances to Celery, which is fragile and can serialize stale/heavy objects.
- It calls `timezone.now()` repeatedly.
- It iterates all overdue rows without batching.
- It returns `overdue.count()` after iteration, causing another query.

Why local testing hides it:

- Small local overdue sets do not show memory or batching issues.
- Manual task runs are usually single runs, not retries after partial failure.
- Eager Celery mode hides serialization and worker process behavior.

Fix:

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

What would catch it:

- Idempotency test that runs the task twice.
- Retry simulation after partial failure.
- Load test or review for `.iterator()` and task argument serialization.

## Part C - Optimise PostgreSQL Query

Original issue:

```sql
WHERE DATE(c.checked_out_at) BETWEEN '2026-01-01' AND '2026-06-30'
```

This wraps the indexed column in a function, making a normal btree index on `checked_out_at` less useful. `SELECT *` also returns more data than a reporting screen likely needs.

Rewrite:

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

Changes:

- Use a half-open timestamp range instead of `DATE(...)`.
- Use `JOIN` instead of `IN` so the planner has a direct join path.
- Select only needed columns.
- Keep `returned_at IS NULL` because the report only needs open check-outs.

Indexes:

```sql
CREATE INDEX CONCURRENTLY idx_checkouts_open_checked_out_due
ON checkouts (checked_out_at, due_at)
WHERE returned_at IS NULL;

CREATE INDEX CONCURRENTLY idx_employees_active_id
ON employees (id)
WHERE is_active = true;
```

The partial checkout index earns its place because the query always filters to open rows. It is smaller than indexing all 4.2M rows and helps the date range plus ordering. The employee partial index may help only if inactive employees are common; if almost everyone is active, it may not be worth keeping because the primary key lookup after joining from filtered checkouts may be enough.

Expected `EXPLAIN (ANALYZE, BUFFERS)` before:

- Sequential scan or very large scan on `checkouts`.
- Filter line showing `date(checked_out_at)` and many rows removed by filter.
- High shared buffer reads/hits.
- Sort node over many rows.

Expected after:

- Bitmap index scan or index scan using `idx_checkouts_open_checked_out_due`.
- Far fewer rows removed by filter.
- Lower buffer reads.
- The line proving the fix worked is the scan on `checkouts` using the new partial index.

Growth risk:

- The open-checkout working set and report sort will break first as rows grow.
- Before that happens, monitor query plans, add pagination/limits for the report, archive old returned rows if needed, and consider partitioning by date only if retention/reporting needs justify it.

Measurement needed:

- Measure selectivity: how many rows have `returned_at IS NULL`, how many fall in the date range, and what percentage of employees are active. Without that, index choice is educated but not certain.

## Part D - Production Reasoning

### D1 - Zero-downtime migration

Use multiple deploys. First deploy adds `location_id` as nullable, with the foreign key constraint added in a way that avoids long blocking validation. Old code keeps working because it does not know about the column and the column accepts null. New code can start writing `location_id` for new check-outs while still tolerating null for old rows.

Then backfill existing rows in small batches outside the request path. Do not run one huge transaction over 4.2 million rows. Track progress and pause if locks or replication lag rise.

After backfill, deploy code that requires and validates `location_id` for all new writes. Once metrics show no null writes remain, add the final database constraint: validate the FK constraint and set the column non-null. The dangerous mistake is adding a non-null column with a default or validating a large FK in one blocking operation, because it can rewrite or lock the table and block writes.

Rollback: during the nullable phase, old code still works. After the app requires `location_id`, rollback only to code that tolerates both null and non-null until the final non-null constraint is in place.

### D2 - Latency triage

First check whether the database is slow or the app is slow: endpoint timing, DB query duration, worker CPU, memory, and connection pool saturation. Then check PostgreSQL activity for blocking locks, long transactions, autovacuum, stale stats, and whether the query plan changed. Next check row counts: open overdue check-outs may have grown sharply. Then check infrastructure: disk IO, CPU steal, network latency, and recent DB maintenance.

Because no deploy happened in nine days, the two most likely causes are data shape change or database health change. Data shape change means many more open overdue rows than before; confirm with counts by due date and returned status. Database health change means stale stats, bloat, lock contention, missing autovacuum, or IO pressure; confirm with `pg_stat_activity`, `pg_stat_user_tables`, `EXPLAIN (ANALYZE, BUFFERS)`, and host metrics.

### D3 - CI/CD and safety

On pull requests, run formatting/lint checks, unit tests, API tests, migration checks, and a Docker build. Use a PostgreSQL service in CI so transaction and locking behavior are tested against the real target database.

On merge to main, build and push an immutable Docker image, run the test suite again, and deploy first to staging. Production deploy requires passing tests, successful staging smoke tests, and approval.

For migrations, expand before contract. Backward-compatible migrations run before new code goes live. New code must tolerate both old and new schema during rollout. Destructive or restrictive changes happen in a later deploy after all app instances are updated and data is backfilled.

Rollback depends on migration type. If the schema was expanded, rollback code safely. If a restrictive migration has already run, do not blindly roll back to code that cannot handle the new schema. Roll forward with a fix or use a prepared backward-compatible rollback version.
