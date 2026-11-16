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
| `DAGONWEB_SMTP_HOST` | unset | SMTP host for registration verification email. |
| `DAGONWEB_SMTP_PORT` | `587` | SMTP port. |
| `DAGONWEB_SMTP_FROM` | `noreply@localhost` | Sender address for verification email. |
| `DAGONWEB_SMTP_USER` | unset | SMTP username. |
| `DAGONWEB_SMTP_PASSWORD` | unset | SMTP password. |
| `DAGONWEB_SMTP_TLS` | `true` | Whether to use STARTTLS. |

The admin setup panel stores the scratch directory and SMTP settings in the database. The executor resolves the scratch path and rejects file browsing outside the configured scratch root. Environment SMTP variables override database values when present.

Each workflow has a **Setup** page with three groups of settings:

- **Common `dagon.ini` values**: batch thread count, DAGon service route and use, and FTP public host.
- **DAGonStar runtime options**: `max_threads` (1–256, default 10), `portable_emulation`, and `pre_stage`. These are stored in the additive `dagonweb.runtime` extension, and `pre_stage` is also written as the top-level DAGonStar `pre_stage` field.
- **Raw per-workflow `dagon.ini`**, in the advanced section. Sections the guided form does not manage, such as `[agent.*]` and `[agent_provider.*]` for Agentic tasks or `[llm.*]` for LLM tasks, are kept when you save the form.

Admins can edit the global persistent `dagon.ini` from **Admin → Setup → Edit global dagon.ini**. By default it is stored at `instance/dagon.ini`; set `DAGON_INI_PATH` to use another persistent location. The editor provides guided controls for DAGon service registration, FTP publication, DAGon IP, batch staging, Slurm, cloud provider placeholders, OpenAI-compatible `[llm.<name>]` providers, logging, and a raw INI view. Global service, executor, and logging sections are used as defaults for new workflow runs, then per-workflow settings are overlaid. DAGonWeb always overrides `[batch] scratch_dir_base` and `remove_dir` so runs and file browsing remain inside the configured scratch root.

New users register with an email address only. DAGonWeb stores a hashed registration token for 24 hours and sends a verification link. Following that link lets the user set a password; if SMTP is not configured, the link is written to the application log.
