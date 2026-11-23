# Dell-PLX cutover

Nothing in this change applies these commands. Funnel stays off.

amber-web's unit is `docs/ops/amber-web.service`. It listens on `127.0.0.1:8084`. It is shipped disabled. The cold-boot check of port 8084 only works after the unit is enabled, and that enable happens before `tailscale serve` is pointed at 8084.

## 1. Enable amber-web on loopback

Copy `docs/ops/amber-web.env.example` to `~/.config/amber/amber-web.env`, mode `0600`, with only `AMBER_READER_PASSWORD`. Do not use `amber-reader.env` here. That file can set data paths and ports, and those would override this unit.

```bash
systemctl --user enable --now amber-web.service
curl -fsS -u "amber:$AMBER_READER_PASSWORD" http://127.0.0.1:8084/healthz
```

Port 443 still serves the reader on 8081. 8081 stays the rollback target until Phase 7. The restore drill uses port 18081, not 8081.

## 2. Cold boot

Run `docs/ops/dell-plx-cold-boot.md` while 443 still points at 8081 and 8443 still points at 8082. amber-web is checked on loopback because it is not on the tailnet yet.

## 3. Serve, after the cold boot passes

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8084
tailscale serve --bg --https=8443 http://127.0.0.1:8082
tailscale serve --bg --https=8444 http://127.0.0.1:8083
```

Do not run `tailscale funnel`. `scripts/health-amber.sh` fails if funnel status is anything other than a known off state.

Current serve, left in place until step 3:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8081
tailscale serve --bg --https=8443 http://127.0.0.1:8082
tailscale serve --bg --https=8444 http://127.0.0.1:8083
```
