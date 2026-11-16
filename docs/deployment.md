# Deployment

DAGonWeb ships one image and a set of Compose files that can be combined:

| File | Adds |
| --- | --- |
| `docker-compose.yml` | The web application with SQLite and in-process run threads. |
| `docker-compose.postgres.yml` | PostgreSQL 16 (standalone or as an overlay). |
| `docker-compose.mysql.yml` | MySQL 8.4 (standalone or as an overlay). |
| `docker-compose.redis.yml` | Redis 7 and scalable run workers. |

Every service reads an optional `.env` file. Start from the example:

```bash
cp .env.example .env    # set SECRET_KEY and the first admin account
```

## SQLite (single host, default)

```bash
docker compose up --build
```

Open http://localhost:8000. Two host directories are mounted:

```yaml
volumes:
  - ./instance:/app/instance      # SQLite database, dagon.ini, logo
  - ./scratch:/scratch/dagonweb   # run directories
```

## PostgreSQL or MySQL

```bash
docker compose -f docker-compose.postgres.yml up --build
docker compose -f docker-compose.mysql.yml up --build
```

## Redis queue for many concurrent runs

By default each run executes in a thread of the web process, limited by **Admin → Execution → Concurrent runs per process**. Runs stop if the web process restarts. For heavy or long workloads, switch to the Redis dispatcher: the web application only enqueues runs, and dedicated workers execute them.

```bash
# put DATABASE_URL=postgresql+psycopg2://dagonweb:dagonweb@db:5432/dagonweb in .env
docker compose -f docker-compose.yml -f docker-compose.postgres.yml \
               -f docker-compose.redis.yml up --build --scale worker=4
```

- Each worker runs one workflow at a time (DAGonStar still runs that workflow's tasks in parallel, up to `max_threads`). Scale the `worker` service for more concurrent runs.
- Workers need the same database and the same scratch volume as the web service. Use PostgreSQL or MySQL: SQLite allows a single writer. On several hosts, put the scratch root on shared storage (NFS, Lustre, CephFS…).
- Runs survive web restarts, and each run is limited by **Run timeout (hours)**. If a worker process is killed mid-run, the run can stay marked as running; delete it and start it again.
- **Admin → Execution** shows whether Redis answers, the queue length and the number of workers.
- If Redis is unreachable when a run starts, DAGonWeb logs a warning and runs it in a local thread, so runs are never lost.

Without Docker, set the backend and start workers yourself:

```bash
export DAGONWEB_EXECUTION_BACKEND=redis REDIS_URL=redis://localhost:6379/0
flask --app wsgi worker            # add --burst to exit when the queue is empty
```

## Container entrypoint

The image's entrypoint applies migrations, seeds the roles and the first admin, and starts Gunicorn (`GUNICORN_WORKERS`, `GUNICORN_THREADS`). Any other command replaces Gunicorn, for example `docker compose run --rm dagonweb flask --app wsgi cleanup-runs`.

Images are published to `ghcr.io/<owner>/dagonweb` by the `Publish container image` workflow on every push to `main` and on `v*` tags (set `DAGONWEB_IMAGE` to use one instead of building).

## Production checklist

- Put the application behind a reverse proxy that terminates TLS and forwards to port 8000 (SSO providers require HTTPS redirect URIs).
- Set a long random `SECRET_KEY` and change the seeded admin password.
- Use PostgreSQL or MySQL, back up the database and the `instance` directory.
- Schedule `flask --app wsgi cleanup-runs` if you set a retention period.
