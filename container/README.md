# Native gPodder Web

This fork adds a real HTTP server and a responsive HTML/JavaScript interface on
top of **gPodder's existing headless engine**. It does not run GTK, a desktop,
X11, VNC, or a remote-desktop web client. Nothing needs to be installed in the
browser or on the MP3 player.

## Run with Podman

```sh
git clone https://github.com/Xearus/gpodder.git
cd gpodder
podman compose up --build -d
```

Open **http://localhost:8090**. The original Feedwatch project, if running,
remains separate on port 8000.

Without a Compose provider:

```sh
podman build --format docker -t localhost/gpodder-web -f Containerfile .
podman run -d --name gpodder-web \
  -p 127.0.0.1:8090:8080 \
  -v gpodder-data:/data \
  --restart unless-stopped \
  localhost/gpodder-web
```

Docker: `docker compose up --build -d`.

The application runs as UID 10001. A named volume persists the upstream
gPodder database, settings, downloaded audio, and the web interface's queue
across container replacement. Do not run multiple replicas/workers or the
desktop application against the same data directory.

## Curating the library

1. Add a podcast RSS/Atom URL. The background engine imports its episodes.
2. Select episodes and click **Add selected to Up next**. This both saves the
   ordered queue and schedules their downloads immediately, ahead of transfer time.
3. Open **Up next** to reorder episodes and see ready, queued, downloading,
   or failed states. Failed downloads have a retry action.
4. Use **Play cached audio** to preview a local download without fetching the
   source again. Browser codec support varies.
5. **Mark listened** when finished. This removes the episode from Up next,
   but does not delete its audio or any device files.

You can also download selected episodes without adding them to Up next,
download an entire available feed, or enable automatic downloads of all new
episodes for a feed. Automatic download is off by default to avoid filling the
server unexpectedly; enabling it also schedules current episodes. Disabling it
stops scheduling future episodes but does not cancel downloads already queued.

Polling and downloads keep running when all browser windows are closed.
The UI refreshes every three seconds. Interrupted downloads are rescheduled
on startup and use gPodder's partial-download/resume support where the source
server permits it. Failed downloads require an explicit retry.

**USB transfer is deliberately deferred.** No USB privileges, host device
mounts, automatic copies, or automatic device deletion are configured. The next
step can bind a mounted flash-drive-style MP3 player into the container and add
safe copy/transfer tracking. Charging alone does not guarantee a USB data connection.

There is no total disk quota or audio conversion yet. Downloads retain the
publisher's original format; an MP3-only player may need conversion of AAC/OGG.
Only episodes still available from their publisher can be downloaded. Cached
episodes survive feed rotation; uncached episodes removed upstream remain
visible in the web history but cannot be recovered.

## Server access

The default binds **localhost only** and is intended for a trusted single-user
installation. On a remote server, an SSH tunnel works without exposing the app:

```sh
ssh -N -L 8090:127.0.0.1:8090 your-user@your-server
```

Then open `http://localhost:8090` on your computer.

For direct LAN access, configure an authenticated HTTPS reverse proxy before
changing `GPODDER_BIND` from `127.0.0.1`. Do not expose the app to untrusted
users: subscribed feeds and enclosures can access HTTP(S) addresses reachable
from the server, including private networks. A browser client cannot grant the
server access to a USB device attached to a different computer.

## Configuration

Compose reads these optional variables from the environment or a local `.env`
(ignored by Git):

| Variable | Default | Meaning |
| --- | --- | --- |
| `GPODDER_BIND` | `127.0.0.1` | Host interface used for the published port |
| `GPODDER_PORT` | `8090` | Host web port |
| `GPODDER_WEB_POLL_SECONDS` | `300` | Feed poll interval, at least 10 seconds |

Inside the container, `GPODDER_HOME=/data` stores the upstream `Database`,
`Settings.json`, and `Downloads/`, plus `web.sqlite3` for queue and UI metadata.
The upstream engine runs on one dedicated thread; HTTP handlers use a separate
SQLite read model so downloading a large episode does not block the web UI.
The original desktop source remains in this fork; its GTK/D-Bus dependencies
are now an optional `desktop` extra rather than requirements of the headless
server.

Inspect the running service:

```sh
podman compose ps
podman compose logs --tail 100
curl -fsS http://localhost:8090/health
```

API documentation is at `/docs`. No transfer-to-device endpoint is exposed yet.

## Tests

The `webtest` target adds test dependencies separately from the runtime image:

```sh
podman build --format docker --target webtest -t localhost/gpodder-web:test -f Containerfile .
podman run --rm localhost/gpodder-web:test
```

Web tests use a local HTTP podcast fixture and the actual gPodder model and
download task, not a second feed parser or downloader.
