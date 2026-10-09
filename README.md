# Employee Attendance & Analytics API

FastAPI and MongoDB service for employee records, attendance punches, corrections with audit history, and monthly/daily analytics.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+. Set MONGO_URI and MONGO_DB in PowerShell or a local .env file, install requirements.txt, and run uvicorn app.main:app --port 8000. API docs are at http://localhost:8000/docs.

## Included behavior

Punch-in/out apply IST and overnight-shift rules, grace-period and overtime thresholds, half-up duration rounding, and half-day calculation. Regularizations recompute derived values and append an audit entry. Analytics use MongoDB aggregation pipelines; department trends fill missing dates and compute a seven-day window in MongoDB. The explain endpoint returns execution-statistics plans.

## Checks

Run python -m unittest discover -s tests. These checks do not replace an integration run against MongoDB; write paths and explain plans need a running server.

The repository includes the required app/main.py, requirements.txt, REVIEW.md, and DECISIONS.md. No environment file, credentials, sample seed dump, or Dockerfile is included.
