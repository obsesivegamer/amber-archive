# Dell-PLX cutover

Nothing in this change applies these commands. Funnel stays off.

amber-web's unit is `docs/ops/amber-web.service`. It listens on `127.0.0.1:8084`. It is shipped disabled. The cold-boot check of port 8084 only works after the unit is enabled, and that enable happens before `tailscale serve` is pointed at 8084.

## What is serving now

Leave this in place until step 3. It is also the rollback. Do not run it after step 3: that would point 443 back at 8081.

Import is not on the tailnet. It listens on `127.0.0.1:8083` only. The old `8444` serve route was removed on purpose and is not part of this cutover.

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8081
tailscale serve --bg --https=8443 http://127.0.0.1:8082
```

## 1. Enable amber-web on loopback

Create `~/.config/amber/reader.password`, mode `0600`, containing only the password. Point amber-reader at the same file (`AMBER_READER_PASSWORD_FILE`). Do not use `amber-reader.env` as an `EnvironmentFile` for this unit.

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
```

Do not run `tailscale funnel`. `scripts/health-amber.sh` runs `tailscale serve status --json` and fails if that command fails, the output is not JSON, or any `AllowFunnel` value is true.
