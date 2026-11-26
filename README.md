# EventDesk / NewYearApp

Updated: 2026-11-26

The live staff header shows the configured event title. [TOKIN New Year Party 2027](https://tokinparty27.pages.dev/)

An event check-in app with QR scanning, employee lookup, attendance, prize draws, and a projector view. It runs locally with Python and SQLite. The hosted runtime supports SQL Server as the primary event database through a private Windows API, retaining Cloudflare SQLite as a best-effort outage copy. Online SQL operations continue when that backup reaches its storage quota. Employee photos use the same protected gateway.

During a SQL API outage, a verified Cloudflare copy can serve saved data and queue check-ins. The UI shows **Saved offline** until SQL accepts them. Prize draws and other conflicting changes pause. This requires working access to Cloudflare and available quota; it is not browser-only offline storage. See [the SQL failover guide](cloudflare/SQL_FAILOVER.md).

Under traffic, the Worker schedules backup verification at most once per minute per isolate. Unchanged revisions skip mirror updates. Storage failures pause backup probes for five minutes. If the API and SQLite quota are both unavailable, new offline check-ins cannot be saved. Other Workers Free limits still apply.

The scanner uses html5-qrcode and jsQR. Employee details appear in a popup; successful check-in shows a green animation. Employees who already checked in have an information-only popup. The interface supports reduced motion.

Staff pages pause automatic requests when hidden, unfocused, or inactive for two minutes. A visible projector polls every 15 seconds while waiting and every three seconds while a prize is ready or a draw is in progress. Failed polls back off to 30 seconds, then 60 seconds. Confirmed selections and draws refresh visible projector windows in the same browser immediately. Hiding the projector pauses polling.

## Run locally

Use Python 3.12 or newer on 64-bit Windows, Linux, or Apple silicon macOS. These commands are for PowerShell from the repository root:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe setup.py
.venv\Scripts\python.exe app.py
```

Setup asks for separate administrator and staff passwords, then creates private configuration and a SQLite database in `data/`. No passwords or employee records are supplied with the repository. Open [localhost:8080](http://localhost:8080).

To try the included synthetic employees, stop the server and run this once after setup, then start the server again:

```powershell
.venv\Scripts\python.exe -c "from pathlib import Path; import app; print(app.import_employees(app.get_import_rows('sample_employees.csv', Path('sample_employees.csv').read_bytes()), 'sample-setup'))"
```

On macOS or Linux, use `python3` to create the environment and `.venv/bin/python` for the remaining commands.

To rehearse the seven-digit reels, add a synthetic employee with a numeric ID such as `0000001` and check them in. The IDs in `sample_employees.csv` contain letters.

## Use the app

- Staff can scan a QR code, use a handheld barcode scanner, or enter an employee ID.
- Administrators manage employees, attendance, wristband status, and prizes.
- The projector view is available at `/projector`.
- On the staff **Draw** page, choose **Open projector**. A gold question mark appears on the stage until you select a published prize from **Rewards**. Select a prize and click **Draw** to start the reels. Use **Next prize** to move on or **Clear selection** to restore the question mark.
- If a reorder response is lost, choose **Retry prize order** under **Rewards** in the same signed-in browser session. The app resends the saved order with its original request ID.
- During a draw, seven employee ID boxes stop one by one from left to right. The server selects and saves the winner before the visual reveal. Keep **Show employee ID** enabled in screen settings to display the number on the projector.
- Open **Projector audio** on the staff draw page to adjust music and effects independently for projector windows in the same browser. If playback is blocked, click **Enable sound** on the projector. Press **M** on the projector to mute or unmute. The app preloads its original, royalty-free audio files from the same server as the projector.
- Camera streaming requires HTTPS or localhost. On LAN HTTP, use the photo picker, handheld scanner, or manual entry. Focus, zoom, and torch controls depend on the browser and camera.

The server listens on all interfaces by default. Set `PARTY_HOST=127.0.0.1` to restrict it to this computer. The default port is `8080`.

`PARTY_OPEN_STAFF=1` deliberately opens staff access to everyone who can reach the app. Administrator sign-in stays separate. Leave this variable unset when staff should sign in.

## Employee data

Use the administrator's Add employee action for individual records. For a compatible workbook, the converter reads its `DATA` and `Lucky Wheel` sheets:

```powershell
.venv\Scripts\python.exe scripts\convert_excel_to_json.py path\to\employees.xlsx --output data\employees.json
```

An empty database loads this JSON when the server starts. Converting a workbook does not replace an existing database. Back up the database before changing employee data.

The repository excludes employee files, photos, databases, private configuration, TLS keys, and generated QR exports. Keep them out of Git and public static assets.

## Deployment and photos

See [DEPLOYMENT.md](DEPLOYMENT.md) for HTTPS, Docker, SQL backend installation, migration, photos, and [releasing an update](DEPLOYMENT.md#release-a-tested-update). See [cloudflare/README.md](cloudflare/README.md) for the hosted architecture and bundle format. The Worker defaults to `EVENT_STORAGE=sql`. Explicitly use `sqlite` while preparing a fresh deployment that has no SQL backend. These settings do not change the standalone local SQLite server.

Publishing to GitHub does not deploy the app; no automatic deployment workflow is configured. Private event images and the original font are omitted from this repository. Optional image requests may return 404 until you add your own permitted assets; employee photos use a placeholder when unavailable.

## Checks

Run Python tests in the virtual environment and JavaScript tests with Node.js:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
node --test tests/*.js
```

The optional photo and SQL backend services also have Windows build tests:

```powershell
.\photo_api\build.ps1
```

## Source layout

| Path | Purpose |
| --- | --- |
| `app.py`, `setup.py` | Local HTTP server, business logic, SQLite, initial setup |
| `static/` | Browser interface and vendored scanner libraries |
| `cloudflare/` | Python Worker, SQL coordinator, Durable Object mirror/outbox, asset bundler |
| `eventdesk_storage/` | Native SQL Server provider, query compiler, schema catalog |
| `photo_api/` | Windows photo API, SQL event service, importers, authenticated gateway |
| `scripts/` | Employee conversion, local certificates, maintenance tools |
| `tests/` | Python and JavaScript tests |

Third-party notices remain in `static/vendor/`. The original FC Pride font is not redistributed; the public source uses system fonts. Add a custom font only with permission to redistribute it. This repository does not grant a new license for third-party assets or project code.
