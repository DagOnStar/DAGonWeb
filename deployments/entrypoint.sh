#!/usr/bin/env sh
set -eu
mkdir -p "${SCRATCH_DIR:-/scratch/dagonweb}" /app/instance
flask --app wsgi db upgrade || flask --app wsgi db init && flask --app wsgi db migrate -m init && flask --app wsgi db upgrade
flask --app wsgi seed || true
exec gunicorn -b 0.0.0.0:8000 "wsgi:app"
