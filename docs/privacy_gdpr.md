# Privacy and GDPR

DAGonWeb is designed so that an institution can run it in line with the EU General Data Protection Regulation (GDPR). The administrator remains the data controller and must complete the details below; this page lists what the software does to support them.

## Configure (Admin → Privacy (GDPR))

| Setting | Use |
| --- | --- |
| Data controller, postal address | Shown in the privacy notice (Art. 13(1)(a)). Empty uses the site name. |
| Privacy contact, DPO email | Where users send requests (Art. 13(1)(b)). Public bodies must appoint a DPO (Art. 37). |
| Supervisory authority | Where users can complain, for example *Garante per la protezione dei dati personali* (Art. 77). |
| Privacy notice version | Change it when the notice changes materially: every signed-in user is asked to read and accept the new version before continuing. |
| Additional privacy notice text | Free text appended to the generated notice. |
| Show the cookie banner | Turns the banner on or off (on by default). |
| Delete audit log entries after (days) | Retention of the audit log, default 365. |
| Report accounts inactive for (days) | Lists inactive accounts during clean-up so you can contact or remove them. |

All settings can be pinned with `DAGONWEB_<KEY>` environment variables (see [configuration.md](configuration.md)).

## What the software provides

- **Privacy notice** at `/privacy`, generated from the configuration: processing purposes, legal bases, retention periods, recipients (SMTP, SSO providers, the LLM endpoint and CDNs only when used), and data subject rights.
- **Acknowledgement at registration** and **re-acceptance** when the notice version changes. The accepted version and date are stored on the user (`privacy_version`, `privacy_accepted_at`). SSO accounts see the notice at their first sign-in.
- **Account page** (`/account/`) for every user:
  - *Access and portability* (Art. 15, 20): **Download my data** returns a JSON file with the account, workflows (as DAGonStar documents), runs and their logs, and the user's audit entries.
  - *Rectification* (Art. 16): change the display name; change the password for local accounts.
  - *Erasure* (Art. 17): delete the account after confirming the email address (and password for local accounts). Workflows, runs and their scratch files are removed. Audit entries are kept for their retention period but detached: the user link, the email address and the IP address are removed.
  - *Cluster account* (HPC mode): the export lists the linked Unix user name and the public key, never the private key. Unlinking or erasure deletes the key pair; erasure also asks the cluster to delete the user's run folders.
- **Administrator tools** for requests received by email: *Export data* and *Erase account and data* on each user's page. The last administrator and users with active runs cannot be erased.
- **Retention**: `flask --app wsgi cleanup-runs` (run it daily from cron) deletes finished runs past the run retention, audit entries past the audit retention, used or expired registration links, and lists inactive accounts.
- **Data minimisation**: the guest playground stores nothing on the server; dry runs are checked and discarded. The workflow assistant sends only the workflow document and messages, never run files.
- **Security of processing** (Art. 32): salted password hashes, HttpOnly SameSite cookies (`SESSION_COOKIE_SECURE=true` behind HTTPS), CSRF protection, a 30-day "remember me" limit, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` and `Permissions-Policy` headers, scratch confinement and an audit log.

## Cookies

See `/cookies` for the generated cookie policy. DAGonWeb sets only first-party cookies and browser storage:

| Name | Type | Category | Purpose |
| --- | --- | --- | --- |
| `session` | cookie | necessary | Sign-in, CSRF token, SSO state. |
| `remember_token` | cookie | necessary | Only with “Remember me”, 30 days. |
| `dw_consent` | cookie | necessary | The visitor's cookie choice, 180 days. |
| `dagonweb.playground.*`, `dagonweb.assistant.*` | sessionStorage | necessary | Guest drafts and assistant chat, until the tab closes. |
| `dagonweb.theme`, `dagonweb.inspector-width`, `dagonweb.run-details-width` | localStorage | preferences | Only with consent; removed when consent is withdrawn. |

The banner offers **Accept all**, **Necessary only** (equally prominent) and **Customize**; the choice can be changed at any time from **Cookie settings** in the footer. Without JavaScript no optional storage is used.

Bootstrap, the Monaco editor and Chart.js are loaded from public CDNs (`cdn.jsdelivr.net`, `cdnjs.cloudflare.com`). They set no cookies but receive visitors' IP addresses; the notice discloses this. To avoid it, serve these files from your own host.

## Your responsibilities as controller

- Fill in the controller and contact details, and adapt the notice text to your institution.
- Keep a record of processing activities (Art. 30) and sign a data processing agreement with your hosting provider and, if you enable it, the LLM provider.
- Run `cleanup-runs` regularly and choose retention periods that fit your policies.
- Answer requests within one month (Art. 12(3)).
