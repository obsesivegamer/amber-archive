# Dell-PLX cutover serve config

This is the target. Nothing in this change applies it. Do not run these commands until the cutover is accepted.

amber-web stays disabled until then. Its unit is `docs/ops/amber-web.service`. It listens on `127.0.0.1:8084` only. Funnel stays off.

## Current

Leave these as they are:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8081
tailscale serve --bg --https=8443 http://127.0.0.1:8082
tailscale serve --bg --https=8444 http://127.0.0.1:8083
```

`tailscale funnel status` must not report funnel on. `scripts/health-amber.sh` checks that.

## Target

Point the phone origin at amber-web. Ingest and import stay on their own HTTPS ports.

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8084
tailscale serve --bg --https=8443 http://127.0.0.1:8082
tailscale serve --bg --https=8444 http://127.0.0.1:8083
```

Do not run `tailscale funnel`. After the serve change, `https://dell-plx-reader.tailf9612a.ts.net/` is amber-web. The reader on 8081 is still there for the restore drill and is no longer the public origin.

Enable the unit only at that moment:

```bash
systemctl --user enable --now amber-web.service
```
