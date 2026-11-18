#!/usr/bin/env sh
# Container entrypoint: apply migrations, seed roles and the first admin, start Gunicorn.
# Pass another command (for example `flask --app wsgi worker`) to run it instead.
set -eu
mkdir -p "${SCRATCH_DIR:-/scratch/dagonweb}" /app/instance
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
flask --app wsgi db upgrade
flask --app wsgi seed || true
exec gunicorn -b 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-2}" --threads "${GUNICORN_THREADS:-4}" --timeout 120 "wsgi:app"
