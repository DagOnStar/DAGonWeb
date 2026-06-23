# AGENTS.md

## Project
DAGonWeb is a Flask/Bootstrap Progressive Web Application for creating, editing, and locally executing DAGonStar-style workflows with a Galaxy-inspired visual editor.

## Goals
- Keep the project simple, readable, and deployable with Docker Compose.
- Preserve the workflow data model: workflows contain tasks and directed links; each link creates a `workflow://<task>/<output>` dependency expression.
- Use SQLAlchemy models and migrations for persistent state.
- Use role-based access: `admin` can manage users, roles, workflows, and settings; `user` can manage own workflows and runs.
- Treat scratch directories as potentially sensitive. Never expose arbitrary filesystem paths outside the configured scratch root.

## Commands
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
flask --app wsgi db upgrade
flask --app wsgi seed
flask --app wsgi run
```

Docker:
```bash
docker compose up --build
```

Tests:
```bash
pytest
```

## Style
- Python: type hints where practical, small functions, no hidden global mutable state.
- Templates: Bootstrap 5, accessible labels, progressive enhancement.
- JavaScript: plain browser JavaScript, no build step required.
- Security: use Flask-WTF CSRF for normal forms; keep API mutations behind login and authorization.

## Review checklist
- Does the change preserve ownership/admin permissions?
- Does the executor keep all files inside the configured scratch directory?
- Does the visual editor save `workflow://` references for links?
- Does the PWA still load `manifest.webmanifest` and register the service worker?
