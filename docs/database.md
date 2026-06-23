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

Flask-Migrate/Alembic commands:

```bash
flask --app wsgi db migrate -m "describe change"
flask --app wsgi db upgrade
```
