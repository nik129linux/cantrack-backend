# CanTrack Backend

Python 3.12+ / FastAPI API with Supabase authentication, PostgreSQL and private storage.
Includes handwritten data structures, walk requests, live GPS, pickup/return photo checks,
dog reference photos and care details, reports and account management.

## Local setup

Clone `cantrack-backend` and `cantrack-db` as sibling directories. The backend tests load
the database repository's schema to validate columns and isolate users.

```sh
cd apps/api-py
cp .env.example .env
uv sync --frozen
uv run --no-sync python -m cantrack_api.main
```

Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `WEB_ORIGIN=http://localhost:5173`.
Leave `ENV` unset for local development. The API runs on http://localhost:3000.
The locked `cantrack-ai` dependency comes from the team's GitHub repository.

## Verification

From `apps/api-py`:

```sh
uv run pytest ../../tests/py -q
```

Tests use in-memory Supabase and AI fakes. The suite includes state transitions,
authorization/isolation, photo checks, authentication rate limits, data structures and
Schemathesis-generated API cases. Test success does not verify live AI or production credentials.

## Deployment

See [the deployment guide](docs/DEPLOYMENT.md). Build using `apps/api-py/Dockerfile`.
The historical [Railway guide](docs/DEPLOY.md) is retained as an alternative;
the recorded production environment uses AWS Lightsail, CloudFront and Supabase.
Postman collections and a photo fixture are under `postman/`.
Security review evidence under `evidencias/security/2026-11-21` is historical, not a new audit.

## Project repositories

| Component | Repository |
|---|---|
| Frontend | https://github.com/nik129linux/cantrack-frontend |
| Backend | https://github.com/nik129linux/cantrack-backend |
| Database | https://github.com/nik129linux/cantrack-db |
| AI package | https://github.com/nik129linux/cantrack-ai |
