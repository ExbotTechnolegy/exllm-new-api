# EXAI PostgreSQL migration

The production Compose layout follows the [New API deployment guide](https://docs.newapi.pro/zh/docs/installation/deployment-methods/docker-installation). New API connects to private PostgreSQL and Redis services. Only New API publishes a port, bound to host loopback. Keep the existing Nginx and payment bridge on the host. The public hostname, API paths and payment callbacks stay the same.

## Prepare the release

1. In [EXLLM product build](../../docs/exllm-product-release.md), choose the approved `exllm-dev` commit and verify the downloaded archives against their manifest. Load the Linux amd64 app image with `docker load -i new-api-image-linux-amd64.tar.gz` and verify its commit label. A deployment may instead pull the same image by digest from a private registry. Approve and pin PostgreSQL 15 and Redis images by digest.
2. On the release host, set the following values through the private deployment configuration. Keep the deployment files, backups and logs accessible only to release operators.

   | Setting | Value |
   | --- | --- |
   | `EXAI_IMAGE`, `EXAI_POSTGRES_IMAGE`, `EXAI_REDIS_IMAGE` | Approved immutable image references. A locally loaded app image can use its full commit tag after checking the image ID and revision label. |
   | `EXAI_APP_ENV_FILE` | Absolute path to the existing application environment copied into a private release file. Preserve the effective secrets for sessions and encryption and the existing proxy, origin and runtime settings. Check for a separate `LOG_SQL_DSN` before proceeding. |
   | `EXAI_POSTGRES_DB`, `EXAI_POSTGRES_USER`, `EXAI_POSTGRES_PASSWORD` | Fresh PostgreSQL database and credentials. Set the application DSN to this same isolated database. |
   | `EXAI_SQL_DSN`, `EXAI_REDIS_URL`, `EXAI_REDIS_PASSWORD` | Authenticated service URLs for Compose hosts `postgres` and `redis`. URL-encode credentials where needed. Use a fresh Redis namespace or instance. |
   | `EXAI_HTTP_PORT` | Use a separate loopback port for rehearsal. For cutover, bind the loopback port already used by the Nginx New API upstream. |

3. Use one Compose project name and keep it stable across operations. Validate without rendering secrets into CI logs. Start once in isolation to let the approved application initialize PostgreSQL, confirm health, then stop New API before importing.

   ```bash
   docker compose -p exai --env-file "$PRIVATE_COMPOSE_ENV" -f deploy/postgres/compose.yml config --quiet
   docker compose -p exai --env-file "$PRIVATE_COMPOSE_ENV" -f deploy/postgres/compose.yml up -d --wait
   docker compose -p exai --env-file "$PRIVATE_COMPOSE_ENV" -f deploy/postgres/compose.yml stop new-api
   ```

4. Rehearse on a representative schema with synthetic data. If using a real snapshot later, use the approved restricted migration environment. Keep PostgreSQL out of public traffic until the SQLite and PostgreSQL checks pass. If a separate log database is configured, inventory it separately and confirm log continuity. This importer handles one SQLite file and one PostgreSQL database per run.

## Take the final snapshot and import

1. Announce the maintenance window. Block new recharge creation, registration, console mutations and billable API traffic. Let existing calls and subscription settlement drain. Leave the payment callback route available while existing WeChat and Alipay orders settle. Reconcile bridge orders against merchant status and New API top-ups by order number. Identify every pending, paid-but-uncredited and unacknowledged callback before freezing writes.
2. Agree how any unresolved callback will be delivered after the freeze. Only proceed with zero unresolved orders or a tested durable retry/replay process. In freeze mode, retain incoming callbacks durably or return a retryable failure according to the verified provider and bridge behavior. Never acknowledge an unrecorded payment. Pause replay to the old application, scheduled jobs that write SQLite, and all other writers. Keep the bridge and its order store intact.
3. Stop the old New API service. Confirm no process can still write SQLite. Take a fresh SQLite backup with the SQLite backup API or `.backup`; do not copy only the live database file while WAL writes are possible. Preserve the pre-cutover application and static release pointers and take a restricted backup of the bridge order store using its supported backup procedure.
4. Import the final **standalone** snapshot. Run the importer in the private environment with access to the Compose PostgreSQL network. The operator needs Python 3.11+ and `psycopg[binary]`. If using a temporary container, join the Compose network, mount the reviewed script and snapshot read-only, and inject the DSN via a private env file. Keep the New API container stopped throughout the import.

   ```bash
   # EXAI_SQL_DSN is already injected by the private migration environment.
   python3 deploy/postgres/import-sqlite.py "$SQLITE_SNAPSHOT" --target-db "$TARGET_DATABASE" --replace-bootstrap
   python3 deploy/postgres/import-sqlite.py "$SQLITE_SNAPSHOT" --target-db "$TARGET_DATABASE" --replace-bootstrap --apply
   ```

   `--replace-bootstrap` is only for PostgreSQL created for this migration, whose existing rows are disposable startup data. It replaces matched tables transactionally. The first command validates the actual copy and rolls it back. It fails on missing tables/columns, nonempty target-only tables or conversion errors. Resolve schema differences in rehearsal. Do not ignore missing tables or columns. The importer checks row counts and compares accounting fields per record, then restores PostgreSQL ID sequences.

## Accept and switch traffic

1. Compare counts and samples against the frozen SQLite snapshot. Check account IDs, roles and state, Key ownership, group, expiry and remaining quota, exact wallet balances, recharge IDs and status, subscriptions and consumption logs. Verify credential continuity with an authorized synthetic or test credential. Keep keys out of the report. Cross-check payment amount and order status against the bridge and merchant records. Confirm critical options, routes and model pricing.
2. Start the new application with PostgreSQL and Redis on loopback. Confirm container health, `/api/status`, login, Key management and **read-only** access to existing balances, recharge history and usage. Verify the current homepage, model market and documentation are unchanged. Do not use a production inference request merely as a smoke test.
3. Cut the existing Nginx upstream over to the healthy container. Keep the public route for the payment bridge. If switching loopback port, validate the private Nginx configuration before reloading. Reconnect the approved bridge callback and replay/reconcile queue to the new New API, verify existing notifications remain idempotent, and check for uncredited payments. Resume writes and new payments only after approval. Leave the SQLite-specific expiry job stopped until its PostgreSQL-compatible replacement is verified.
4. Record the source commit, image identifiers, snapshot timestamp, release pointers, verification results and decision maker in the private release record. Monitor callback failures, stuck orders, database errors and usage settlement. Take the first PostgreSQL backup and test its restore on an isolated target.

## Rollback decision

- **Before PostgreSQL accepts any customer writes, including a callback replay**, the authorized release operator may freeze routes again and restore the old application and SQLite snapshot, then reopen callbacks only after reconciliation. Maintain one active writer.
- **After PostgreSQL accepts registrations, Key changes, consumption or payment callbacks**, SQLite is stale. Keep payments and writes paused if needed and repair forward on PostgreSQL, or perform a separately reviewed restore/reconciliation that preserves all new transactions. Never reopen the old SQLite copy without reconciling merchant payments, bridge delivery state, balances and usage.
- Production deployment and cutover require an authorized release owner, private release receiver, verified backups and a maintenance window. The repository workflow only creates build artifacts.
