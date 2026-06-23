# Deployment

## SQLite demo deployment

```bash
cp .env.example .env
mkdir -p instance scratch
docker compose up --build
```

The scratch directory is mounted as an external physical directory:

```yaml
volumes:
  - ./scratch:/scratch/dagonweb
```

## PostgreSQL

```bash
docker compose -f docker-compose.postgres.yml up --build
```

## MySQL

```bash
docker compose -f docker-compose.mysql.yml up --build
```

For production, put the application behind a reverse proxy that terminates TLS and forwards to port 8000.
