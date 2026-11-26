# Cloudflare hosting

Updated: 2026-11-26

This directory contains the hosted runtime and a deterministic asset bundler. Deployment requires resources in your own Cloudflare account. No account identifiers, credentials, Wrangler configuration, or GitHub deployment workflow are included.

## Runtime

```text
Pages entry -> Python Worker -> ASSETS KV (static files)
                           -> protected gateway -> private Windows API -> SQL Server
                           -> STATE Durable Object (coordinator, SQLite mirror/outbox)
                               -> protected gateway -> private Windows API -> SQL Server
```

[pages-entry.mjs](pages-entry.mjs) forwards requests through the `EVENTDESK` service binding. [worker.py](worker.py) is the Python entry point and exports the `EventDesk` Durable Object class. The application uses the object instance named `main`; retain its namespace and identity across releases.

The public [TOKIN New Year Party 2027](https://tokinparty27.pages.dev/) Pages project uses [tokinparty27-pages.mjs](tokinparty27-pages.mjs) as its advanced-mode entry and binds `EVENTDESK` to the same Worker as [the canonical EventDesk entry](https://eventdesk.pages.dev/). The entry validates its public origin and forwards approved requests to the canonical origin through the service binding, preserving session, CSRF, and operation ID checks. Both addresses use the same SQL data and private tunnel. Hosted cookies are limited to the browser's current host, so sign in on the address you plan to use and keep an uncertain operation on its original address and session.

In SQL mode, core requests use the authenticated PhotoPilot API directly from the Worker. The original Cloudflare SQLite database serves as a best-effort mirror and durable recovery queue. Under traffic, backup verification runs at most once per minute per isolate and pauses for five minutes after storage failures. Unchanged revisions skip mirror updates. Explicit SQLite mode keeps the original local handler path inside synchronous Durable Object transactions. The standalone `app.py` server remains separate.

| Module | Responsibility |
| --- | --- |
| `direct_api.py` | Core SQL routes, optional backup scheduling, and signed check-in fallback |
| `event_runtime.py` | Fixed authenticated event transport and atomic mirror/queue boundaries |
| `sql_online.py` | Active quota-tolerant SQL routing, optional backup maintenance, and receipt-safe recovery. |
| `event_primary.py` | SQL-first routing, revision synchronization, pending check-ins, receipt recovery |
| `event_policy.py` | Session, role, origin, and CSRF validation before durable mutation intents |
| `replica.py` | Validated mirror changes and operation-owned outbox chunks |
| `sqlite_adapter.py` | SQLite interface over Durable Object SQL |
| `handler_adapter.py` | Shared `app.py` HTTP business handlers without a socket |
| `hosted_storage.py` | Durable event-image storage |
| `build_bundle.py` | Explicit runtime and static-asset packaging |

| Binding | Type | Value |
| --- | --- | --- |
| `STATE` | Durable Object | SQLite-backed `EventDesk` coordinator; retain the existing namespace |
| `ASSETS` | KV namespace | Content-addressed asset chunks from the build |
| `APP_CONFIG` | Secret | Private setup configuration JSON, including signing secret and password hashes |
| `PUBLIC_ORIGIN` | Plain text | The Worker's canonical HTTPS origin. It remains `https://eventdesk.pages.dev` for the hosted event. |
| `STATIC_MANIFEST` | Plain text | Contents of generated `static-manifest-binding.txt` |
| `DEPLOYMENT_KEY` | Temporary secret | Random bootstrap key of at least 32 characters |
| `PHOTO_PILOT` | Service | Protected gateway for photos and fixed event routes |
| `PHOTO_PILOT_ACCESS_TOKEN` | Secret | Gateway edge bearer token, never sent to browsers |
| `EVENT_STORAGE` | Plain text | Defaults to `sql`; explicit `sqlite` for staging or controlled rollback |
| `EVENT_MAINTENANCE` | Plain text | `1` rejects hosted `/api` requests before database work |

Keep `APP_CONFIG` private even though passwords are hashed. The hosted adapter explicitly enables public staff access in `EventDesk.bind_database`; anyone who can reach it can perform staff actions. Administrator sign-in stays separate. Review that policy before hosting the app.

Use your own canonical HTTPS origin, for example `https://eventdesk.example.invalid`. For a new deployment without the private SQL backend, set `EVENT_STORAGE=sqlite` explicitly; omitting it selects SQL. Keep API maintenance enabled throughout a final export/cutover.

## Build and publish

Install the Python dependencies and run setup as described in [../README.md](../README.md), then build:

```powershell
.venv\Scripts\python.exe -m cloudflare.build_bundle
```

The ignored `data/cloudflare-deploy/` output contains:

- `module-manifest.json`: entry module, runtime module names, and asset chunk names.
- `worker.py`, `app.py`, `vendor.zip`: the Python Worker modules.
- `assets/`: public file chunks for the `ASSETS` KV namespace.
- `static-manifest.json` and `static-manifest-binding.txt`: asset mappings and the compressed binding value.

The vendor archive contains the hosting adapters, metadata-only `eventdesk_storage` table catalog, and pure-Python dependencies. Native SQL drivers and the SQL query compiler belong in the protected Windows backend package, not the Cloudflare archive. Private image and font files are omitted from public source. On the existing deployment, preserve the authorized FC Pride font entry and its verified KV chunks when merging a new static manifest. A fresh public build can use its system font fallback.

Publish the listed modules using the Cloudflare API's Python Worker deployment support. Upload `vendor.zip` as `application/octet-stream`, stage and verify missing KV chunks, and keep the existing secret and service bindings. If using Pages for the public address, publish the designated entry as the advanced-mode `_worker.js` and bind `EVENTDESK` to your Worker. Follow the [release order](../DEPLOYMENT.md#release-a-tested-update) for changes that update both the Worker and the private API. Provision and review resources using your account tooling.

Only upload the explicit module and asset outputs. Never publish the complete `data/` directory, which can contain private configuration and migration material.

Initial state import uses the guarded `/__deployment/import` and `/__deployment/complete` routes in [worker.py](worker.py). Import batches are limited to 100 rows and validated against supported table schemas. Complete the bootstrap only after expected counts and foreign keys validate; normal application requests require initialized state. Keep any migration payload private and remove `DEPLOYMENT_KEY` after initialization. Future code updates should reuse the existing initialized object instead of importing attendance again.

The original bootstrap is separate from a SQL cutover. Do not rerun it on an initialized deployment. Follow [the cutover steps](../DEPLOYMENT.md#migrate-and-cut-over) and [SQL_FAILOVER.md](SQL_FAILOVER.md): freeze traffic, export the authoritative hosted data, import into empty native SQL tables, validate/seed replica metadata, and only then switch to SQL. Preserve identities, QR tokens, sessions, histories, images, and the signing secret.

Local SQLite data is not automatically synchronized to Cloudflare. Hosted statistics may be cached briefly; employee lookup and check-in use the selected storage path.

## SQL outage and recovery

The native backend supports SQL Server 2014 and compatibility level 120. It uses the original `app.py` business rules with `eventdesk_storage`. The Windows supervisor listens through its Python child at loopback port `8766`, behind the existing C# API at loopback port `8765`. Only authenticated `/v1/event/request` and `/v1/event/snapshot` are proxied. The tunnel does not expose a general SQL endpoint.

Healthy writes atomically commit SQL receipts and business changes. Committed revisions keep the Cloudflare mirror current. Mutations use stable UUIDs; a timeout after sending a write has an unknown result until receipt reconciliation resolves it. A new UUID is not a safe way to retry an uncertain prize draw.

When the API is unavailable, a verified mirror can serve saved reads and atomically accept a pending check-in with its queue entry. HTTP 202 means **Saved offline**, not confirmed in SQL. Draws, resets, replacements, and conflicting actions halt. Invalid authentication or protocol responses fail closed. Missing photos show a placeholder.

Incoming requests drive recovery: resolve receipts/cancellation tombstones, replay queued check-ins in order with their original timestamps, and apply SQL revisions. There is no background timer, and recovery never overwrites SQL with an old SQLite file. Review rejected entries and seven-day queued-session expiry before changing modes or restoring data.

The fallback requires Cloudflare reachability, a verified mirror, and available quota. If a durable intent cannot be saved, fallback check-ins cannot commit. Healthy direct SQL operations can continue when Durable Object quota is exhausted. The deployment uses Workers Free and remains subject to its request and CPU limits. Rehearse expected load before the event.

## Shared photo and event gateway

The gateway source is [../photo_api/pilot_worker.mjs](../photo_api/pilot_worker.mjs). Its historical filename does not limit it to pilot use. Deploy its imported [event_worker_proxy.mjs](../photo_api/event_worker_proxy.mjs) module alongside it when using SQL event storage; publishing only the entry module is incomplete.

| Gateway binding | Type | Purpose |
| --- | --- | --- |
| `PHOTO_API` | VPC service | Private Windows API reached through your tunnel |
| `PHOTO_API_TOKEN` | Secret | Backend API bearer token |
| `PILOT_ACCESS_TOKEN` | Secret | Separate edge gateway bearer token |
| `PHOTO_SCOPE` | Plain text | `all-employees` for the full employee set; otherwise pilot scope |
| `PILOT_EMPLOYEE_IDS` | Plain text | JSON array of exactly ten distinct seven-digit ID strings when using pilot scope |

On the main application Worker, bind `PHOTO_PILOT` to the gateway and set `PHOTO_PILOT_ACCESS_TOKEN` to the edge token. Review the fixed gateway origins in `app.py`, `cloudflare/worker.py`, and `cloudflare/event_runtime.py` for your deployment before building. Retain the fixed routes and distinct edge/backend bearer tokens. The main app checks employee membership before requesting a photo. The gateway rejects anonymous requests and bounds JPEG and event envelopes. Original session/CSRF checks still protect event actions.

An optional Pages wrapper is [../photo_api/pilot_pages_worker.mjs](../photo_api/pilot_pages_worker.mjs), using the `EVENTDESK_PHOTO_PILOT` service binding. The Windows API, SQL import process, and service installation are documented in [../DEPLOYMENT.md](../DEPLOYMENT.md).

Online SQL confirmation is separate from `X-EventDesk-Backup` (`current`, `stale`, `unavailable`) and `X-EventDesk-Queue` (`clear`, `pending`, `unknown`). A SQL storage quota does not remove other free-plan limits. If the API also fails while SQLite quota is exhausted, offline check-ins are unavailable. See [SQL_FAILOVER.md](SQL_FAILOVER.md) for snapshot freshness, recovery, and exact browser retry identities.
