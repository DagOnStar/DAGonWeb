# DAGonWeb

DAGonWeb is a Flask/Bootstrap Progressive Web Application for designing and executing DAGonStar workflows through a visual editor.

The application lets users register, manage workflows, place tasks from a palette, connect tasks visually, and run them with the DAGonStar runtime using a configurable scratch directory. Dependencies come from DAGonStar `workflow:///task/path` references.

## Inspiration

DAGonStar is a lightweight Python workflow engine for directed acyclic graphs that can run jobs on local machines, HPC clusters, containers, and clouds. DAGonWeb provides DAGonStar workflow authoring and execution.

## Features

- Flask 3, SQLAlchemy, Flask-Login, Flask-WTF, Bootstrap 5.
- PWA manifest and service worker.
- User registration and authentication.
- Role-based authorization with `admin` and `user` roles.
- CRUD for users, roles, workflow metadata, and workflow graphs.
- Visual editor with draggable nodes, curved connectors, auto-layout, and a resizable/expandable inspector.
- DAGonStar task types: Checkpoint, Batch, Slurm, Cloud, Docker, LLM, Native, and Web.
- DAGonStar JSON import/export and direct Python workflow generation.
- Asynchronous server-side runs with live task status and scratch artifact browsing/downloads.
- Admin setup panel for scratch directory path.
- Docker Compose deployment with an external mounted scratch directory.
- SQLite default, with PostgreSQL and MySQL examples.
- Apache-2.0 license.

## Quick start with Docker

```bash
cp .env.example .env
docker compose up --build
```

Open http://localhost:8000 and sign in with the credentials configured in `.env`.

Default demo values:

- Email: `admin@example.org`
- Password: `admin12345`

The default compose file mounts:

- `./instance` to `/app/instance` for SQLite.
- `./scratch` to `/scratch/dagonweb` for workflow runs.

## Local development

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

## Documentation

See:

- [Getting started](docs/getting_started.md)
- [User manual](docs/user_manual.md)
- [Installation](docs/installation.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Workflow editor](docs/workflow_editor.md)
- [Execution model](docs/execution_model.md)
- [Security](docs/security.md)
- [Deployment](docs/deployment.md)
- [Database examples](docs/database.md)

## License

Apache License, Version 2.0. See [LICENSE](LICENSE).
