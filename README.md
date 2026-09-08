# Field Asset Check-Out Service

Django REST API for tracking physical assets checked out to employees.

## Stack

- Python 3.11
- Django 5.0
- Django REST Framework
- PostgreSQL 15
- Redis
- Celery worker and Celery Beat
- pytest-django

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo_data
.\.venv\Scripts\python.exe manage.py runserver
```

The seed command prints a demo token. Use it like this:

```http
Authorization: Token <printed-token>
```

## Docker Setup

```powershell
docker compose up --build
docker compose exec app python manage.py migrate
docker compose exec app python manage.py seed_demo_data
```

API base URL:

```text
http://localhost:8000/api/v1/
```

## Useful Endpoints

- `GET /api/v1/health/`
- `POST /api/v1/auth/token/`
- `POST /api/v1/assets/`
- `GET /api/v1/assets/?status=AVAILABLE&category=CAMERA&search=cam`
- `GET /api/v1/assets/{id}/`
- `POST /api/v1/checkouts/`
- `POST /api/v1/checkouts/{id}/return/`
- `GET /api/v1/employees/{employee_code}/summary/`
- `GET /api/v1/reports/overdue/`

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

For the PostgreSQL-backed concurrency test, run it through Docker:

```powershell
docker compose exec app python -m pytest tests/test_inventory_api.py -k simultaneous -v
```

Pytest will show `19 deselected` because `-k simultaneous` runs only the one test whose name matches `simultaneous`; the other collected tests are intentionally filtered out.

## Environment Variables

No manual environment setup is required for the default Docker flow; `docker-compose.yml` provides working development values.

For custom local or Docker configuration, see `.env.example`.

Important variables:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DB_ENGINE`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_HOST`
- `DB_PORT`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`

## Assumptions

- DRF Token Auth is used because it is simple and enough for this internal API assignment.
- `/api/v1/auth/token/` is unauthenticated so reviewers can obtain a token.
- `due_at < now` means overdue. An item due exactly at `now` is not overdue.
- `days_overdue` is the floored integer number of days.
- The default SQLite config is only for quick local development. Docker uses PostgreSQL, which is the intended database for concurrency behavior.
- The seed command resets only the known demo assets/checkouts it owns, then recreates deterministic demo data.

## Known Gaps

- This is assignment-grade auth, not a full production authorization model.
- The overdue task records notices but does not send email because the assignment only requires flagging overdue check-outs.

## Screen Recording

Google Drive: https://drive.google.com/file/d/1g81OLqyhpg52wxcv7u6VtpnUUjPjP-Ty/view?usp=sharing

Compressed Version:- https://drive.google.com/file/d/1-K8bdX0uOI6ElpMHdW8IAk1Fp-bBDOqB/view?usp=sharing

Note: If browser playback is still processing, the file can be downloaded from Google Drive.
