# Installation

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Python virtual environment

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
flask --app wsgi db init
flask --app wsgi db migrate -m init
flask --app wsgi db upgrade
flask --app wsgi seed
flask --app wsgi run --debug
```

## Production notes

Use a strong `SECRET_KEY`, a production database, HTTPS, persistent volumes for `/app/instance` and `/scratch/dagonweb`, and a WSGI server such as Gunicorn.
