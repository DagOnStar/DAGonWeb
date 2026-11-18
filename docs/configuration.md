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
| `DAGONWEB_<KEY>` | unset | Pins any administration setting (see below). A pinned setting is read-only in the admin panel. |
| `OPENAI_API_KEY` | unset | Alias of `DAGONWEB_LLM_API_KEY`. |
| `REDIS_URL` | unset | Alias of `DAGONWEB_REDIS_URL`. |
| `GUNICORN_WORKERS` / `GUNICORN_THREADS` | `2` / `4` | Web server processes and threads in the container. |
| `SESSION_COOKIE_SECURE` | `false` | Set to `true` behind HTTPS so session and remember-me cookies are sent only over TLS. |

## Administration settings

Settings edited in the [administration panel](administration.md) are stored in the database. Each one is resolved from the environment variable `DAGONWEB_<KEY>` first, then the database, then the default. Booleans accept `1/0`, `true/false`, `yes/no`, `on/off`.

| Section | Keys |
| --- | --- |
| Appearance | `site_name`, `tagline`, `footer_text`, `default_theme` (`auto`, `light`, `dark`), `brand_color`, `navbar_color` |
| Authentication | `local_login_enabled`, `registration_enabled`, `guest_playground_enabled`, `registration_domains`, `sso_auto_create`, `google_enabled`, `google_client_id`, `google_client_secret`, `google_hosted_domain`, `microsoft_enabled`, `microsoft_tenant`, `microsoft_client_id`, `microsoft_client_secret`, `oidc_enabled`, `oidc_name`, `oidc_discovery_url`, `oidc_client_id`, `oidc_client_secret`, `oidc_scopes` |
| Execution | `execution_backend` (`thread` or `redis`), `redis_url`, `redis_queue`, `max_concurrent_runs`, `run_timeout_hours` |
| Storage | `scratch_dir`, `run_retention_days`, `max_upload_mb` |
| SMTP | `smtp_host`, `smtp_port`, `smtp_from`, `smtp_user`, `smtp_password`, `smtp_tls` |
| LLM connector | `llm_enabled`, `llm_base_url`, `llm_api_key`, `llm_model`, `llm_temperature`, `llm_timeout`, `llm_max_steps` |
| Privacy (GDPR) | `controller_name`, `controller_address`, `privacy_contact_email`, `dpo_email`, `supervisory_authority`, `privacy_policy_version`, `privacy_policy_extra`, `cookie_banner_enabled`, `audit_retention_days`, `inactive_account_days` |
| Accessibility | `accessibility_contact_email`, `accessibility_enforcement`, `accessibility_statement_date` |

For example `DAGONWEB_EXECUTION_BACKEND=redis`, `DAGONWEB_SMTP_HOST=smtp.example.org` or `DAGONWEB_LLM_ENABLED=true`. The scratch directory resolves from `scratch_dir`, then the `SCRATCH_DIR` environment variable. The executor rejects file browsing outside the configured scratch root.

## Workflow setup

Each workflow has a **Setup** page with three groups of settings:

- **Common `dagon.ini` values**: batch thread count, DAGon service route and use, and FTP public host.
- **DAGonStar runtime options**: `max_threads` (1–256, default 10), `portable_emulation`, and `pre_stage`. These are stored in the additive `dagonweb.runtime` extension, and `pre_stage` is also written as the top-level DAGonStar `pre_stage` field.
- **Raw per-workflow `dagon.ini`**, in the advanced section. Sections the guided form does not manage, such as `[agent.*]` and `[agent_provider.*]` for Agentic tasks or `[llm.*]` for LLM tasks, are kept when you save the form.

Admins can edit the global persistent `dagon.ini` from **Admin → dagon.ini**. By default it is stored at `instance/dagon.ini`; set `DAGON_INI_PATH` to use another persistent location. The editor provides guided controls for DAGon service registration, FTP publication, DAGon IP, batch staging, Slurm, cloud provider placeholders, OpenAI-compatible `[llm.<name>]` providers, logging, and a raw INI view. Global service, executor, and logging sections are used as defaults for new workflow runs, then per-workflow settings are overlaid. DAGonWeb always overrides `[batch] scratch_dir_base` and `remove_dir` so runs and file browsing remain inside the configured scratch root.

New users register with an email address only. DAGonWeb stores a hashed registration token for 24 hours and sends a verification link. Following that link lets the user set a password; if SMTP is not configured, the link is written to the application log.
