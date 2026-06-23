# DAGonWeb

DAGonWeb is a Flask/Bootstrap Progressive Web Application for designing and executing DAGonStar-style workflows through a Galaxy-inspired visual editor.

The application lets users register, manage workflows, place tasks from a palette, connect tasks visually, and run workflows locally using a configurable scratch directory. Each visual link creates a DAGonStar-like `workflow://<task>/<output>` relationship that is persisted with the workflow graph.

## Inspiration

DAGonStar is a lightweight Python workflow engine for directed acyclic graphs that can run jobs on local machines, HPC clusters, containers, and clouds. Galaxy provides a mature web experience for visual scientific workflow authoring, including workflows composed from a tool panel and linked steps. DAGonWeb combines these ideas for DAGonStar-style local workflow authoring and execution.

## Features

- Flask 3, SQLAlchemy, Flask-Login, Flask-WTF, Bootstrap 5.
- PWA manifest and service worker.
- User registration and authentication.
- Role-based authorization with `admin` and `user` roles.
- CRUD for users, roles, workflow metadata, and workflow graphs.
- Galaxy-like visual editor with a task palette, workspace, visual links, and task inspector.
- Task types: Input, Bash, Python, DAGonStar Native metadata stub, LLM metadata stub.
- Local execution with per-run and per-task scratch directories.
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
