# Configuration

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SECRET_KEY` | development value | Flask signing key. Change in production. |
| `DATABASE_URL` | SQLite in `instance` | SQLAlchemy connection string. |
| `SCRATCH_DIR` | `/scratch/dagonweb` in Docker | Root path for workflow run directories. |
| `DAGON_INI_PATH` | `instance/dagon.ini` | Persistent DAGonStar configuration file managed through the admin UI. |
| `ADMIN_EMAIL` | `admin@example.org` | Seeded admin email. |
| `ADMIN_PASSWORD` | `admin12345` | Seeded admin password. |
| `ADMIN_NAME` | `DAGonWeb Admin` | Seeded admin display name. |

The admin setup panel stores the scratch directory in the database. The executor resolves that path and rejects file browsing outside the configured scratch root.

Admins can edit DAGonStar's persistent `dagon.ini` from **Admin → Setup → Edit dagon.ini**. By default it is stored at `instance/dagon.ini`; set `DAGON_INI_PATH` to use another persistent location. Its service, executor, and logging sections are used for new workflow runs. DAGonWeb always overrides `[batch] scratch_dir_base` and `remove_dir` so runs and file browsing remain inside the configured scratch root.
