# Configuration

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SECRET_KEY` | development value | Flask signing key. Change in production. |
| `DATABASE_URL` | SQLite in `instance` | SQLAlchemy connection string. |
| `SCRATCH_DIR` | `/scratch/dagonweb` in Docker | Root path for workflow run directories. |
| `ADMIN_EMAIL` | `admin@example.org` | Seeded admin email. |
| `ADMIN_PASSWORD` | `admin12345` | Seeded admin password. |
| `ADMIN_NAME` | `DAGonWeb Admin` | Seeded admin display name. |

The admin setup panel stores the scratch directory in the database. The executor resolves that path and rejects file browsing outside the configured scratch root.
