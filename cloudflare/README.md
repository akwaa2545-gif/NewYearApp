# Cloudflare hosting

Updated: 2026-11-09

This directory contains the hosted runtime and a deterministic asset bundler. Deployment requires resources in your own Cloudflare account. No account identifiers, credentials, Wrangler configuration, or GitHub deployment workflow are included.

## Runtime

```text
Pages entry -> Python Worker -> STATE Durable Object (SQLite)
                           -> ASSETS KV (static files)
                           -> optional protected photo gateway
```

[pages-entry.mjs](pages-entry.mjs) forwards requests through the `EVENTDESK` service binding. [worker.py](worker.py) is the Python entry point and exports the `EventDesk` Durable Object class. The application uses the object instance named `main`; retain its namespace and identity across releases.

| Binding | Type | Value |
| --- | --- | --- |
| `STATE` | Durable Object | SQLite-backed `EventDesk` class |
| `ASSETS` | KV namespace | Content-addressed asset chunks from the build |
| `APP_CONFIG` | Secret | Private setup configuration JSON, including signing secret and password hashes |
| `PUBLIC_ORIGIN` | Plain text | Your canonical HTTPS origin |
| `STATIC_MANIFEST` | Plain text | Contents of generated `static-manifest-binding.txt` |
| `DEPLOYMENT_KEY` | Temporary secret | Random bootstrap key of at least 32 characters |

Keep `APP_CONFIG` private even though passwords are hashed. The hosted adapter explicitly enables public staff access in `EventDesk.bind_database`; anyone who can reach it can perform staff actions. Administrator sign-in stays separate. Review that policy before hosting the app.

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

Publish the listed modules using the Cloudflare API's Python Worker deployment support. Upload the listed chunks to your KV namespace and configure the bindings above. If using Pages for the public address, publish the advanced-mode Pages entry and bind `EVENTDESK` to your Worker. There is no one-command account deployment script in this repository; provision and review these resources with your own account tooling.

Only upload the explicit module and asset outputs. Never publish the complete `data/` directory, which can contain private configuration and migration material.

Initial state import uses the guarded `/__deployment/import` and `/__deployment/complete` routes in [worker.py](worker.py). Import batches are limited to 100 rows and validated against supported table schemas. Complete the bootstrap only after expected counts and foreign keys validate; normal application requests require initialized state. Keep any migration payload private and remove `DEPLOYMENT_KEY` after initialization. Future code updates should reuse the existing initialized object instead of importing attendance again.

Local SQLite data is not automatically synchronized to Cloudflare. Hosted statistics may be cached briefly; employee lookup and check-in use current state.

## Optional photo gateway

The gateway source is [../photo_api/pilot_worker.mjs](../photo_api/pilot_worker.mjs). Its historical filename does not limit it to pilot use.

| Gateway binding | Type | Purpose |
| --- | --- | --- |
| `PHOTO_API` | VPC service | Private Windows API reached through your tunnel |
| `PHOTO_API_TOKEN` | Secret | Backend API bearer token |
| `PILOT_ACCESS_TOKEN` | Secret | Separate edge gateway bearer token |
| `PHOTO_SCOPE` | Plain text | `all-employees` for the full employee set; otherwise pilot scope |
| `PILOT_EMPLOYEE_IDS` | Plain text | JSON array of exactly ten distinct seven-digit ID strings when using pilot scope |

On the main application Worker, bind `PHOTO_PILOT` to the gateway and set `PHOTO_PILOT_ACCESS_TOKEN` to the edge token. Replace the fixed photo gateway origin in `app.py` and `cloudflare/worker.py` with your gateway's HTTPS origin before building. The main app checks employee membership before requesting a photo. The gateway rejects anonymous requests and bounds JPEG responses. It does not forward browser credentials to SQL.

An optional Pages wrapper is [../photo_api/pilot_pages_worker.mjs](../photo_api/pilot_pages_worker.mjs), using the `EVENTDESK_PHOTO_PILOT` service binding. The Windows API, SQL import process, and service installation are documented in [../DEPLOYMENT.md](../DEPLOYMENT.md).
