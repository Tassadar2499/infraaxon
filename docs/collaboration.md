# Local collaboration suite

OpenProject Community replaces Jira; Wiki.js replaces Confluence. Mattermost provides an isolated demonstration channel. All three use the example's PostgreSQL 17 server, with separate databases and users.

```sh
python3 tools/infraaxon.py full
uv sync --frozen
uv run python tools/bootstrap_collaboration.py
WITH_COLLABORATION=1 python3 tools/infraaxon.py register-shop
```

Bootstrap initializes only the containers named by this example. It creates synthetic historical incidents and runbooks, not current incident truth. It preserves generated passwords and tokens in the private `.env`, reuses existing pages/projects/users and avoids duplicate fixture messages.

| Service | Login | Password in `.env` | Agent access |
|---|---|---|---|
| OpenProject, port 18083 | `admin` | `OPENPROJECT_ADMIN_PASSWORD` | Dedicated reader with only `view_work_packages` in the demo project |
| Wiki.js, port 18084 | `admin@infraaxon.local` | `WIKIJS_ADMIN_PASSWORD` | API key tied to a group with page/asset read permissions |
| Mattermost, port 18085 | `infraaxon-admin` | `MATTERMOST_ADMIN_PASSWORD` | Dedicated ordinary user in the demo team and incidents channel |

Mattermost personal access tokens inherit the user's permissions, including posting; this is not a read-only token. InfraAxon's adapter exposes only ping and search, and never supplies the token to the model. Use a separate team/user when connecting real systems. Team membership and the configured channel bound the demonstration search.

Wiki.js API keys expire after 365 days. To rotate a key, revoke it in Wiki.js, remove `WIKIJS_API_TOKEN` from the local `.env`, rerun bootstrap, then replace the saved token in the Wiki.js component’s Secrets field. Registration intentionally preserves existing connector secrets. Other credentials can also be rotated manually. Never publish `.env` or use the demo accounts for external systems.

OpenProject allows `localhost:18083` and the internal `openproject` hostname. PostgreSQL extensions are installed in `pg_catalog`, which survives application schema initialization. A PostgreSQL 16 data volume cannot be mounted into PostgreSQL 17; this example uses a distinct `postgres17-data` volume.

The S3 example uses a separate reader with `ListBucket`, `GetBucketLocation` and `GetObject` only for the `products` bucket. The default demo MongoDB and Redis have no authentication and are confined to the Docker network. Their adapters expose read tools, but this is application-level restriction, not database-enforced RBAC. Configure actual read/monitor credentials when connecting an existing installation.
