# Database examples

SQLite:

```text
sqlite:////app/instance/dagonweb.sqlite
```

PostgreSQL:

```text
postgresql+psycopg2://dagonweb:dagonweb@db:5432/dagonweb
```

MySQL:

```text
mysql+pymysql://dagonweb:dagonweb@db:3306/dagonweb
```

Tables: `users`, `roles`, `user_roles`, `settings`, `audit_logs`, `workflows`, `workflow_tasks`, `workflow_links`, `workflow_runs`, `task_runs`, `registration_tokens`.

Migration `a9b8c7d6e5f4` adds `roles.permissions_json`, the SSO identity columns of `users` (`auth_provider`, `external_id`, `last_login_at`) and the `audit_logs` table. CI applies every migration to SQLite and PostgreSQL and checks that the models and migrations agree (`flask --app wsgi db check`).

Use PostgreSQL or MySQL with the Redis execution backend: several worker processes write to the database at once.

Flask-Migrate/Alembic commands:

```bash
flask --app wsgi db migrate -m "describe change"
flask --app wsgi db upgrade
```
