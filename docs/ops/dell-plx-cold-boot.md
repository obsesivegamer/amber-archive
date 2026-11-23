# Cold boot

This checklist needs Jeremy's yes before anyone reboots Dell-PLX. The change that added it does not reboot the machine.

The existing S4U scheduled task is what starts the session at boot. This checklist does not replace that task. amber-web.service must already be enabled, so port 8084 is up after the reboot without a console sign-in. `tailscale serve` for port 443 still points at the reader on 8081 during this check.

Record the evidence in `~/apps/amber-reader/cold-boot-YYYY-MM-DD.txt` on Dell-PLX. Each check gets a UTC timestamp. Every check below has to finish within 5 minutes of `LastBootUpTime`.

## Before the reboot

Record the auto-sign-in setting, then turn it off.

Settings, Accounts, Sign-in options. The switch is "Use my sign-in info to automatically finish setting up my device after an update or restart". Write down whether it was on or off, then set it off. Write that down too.

## Reboot

Reboot Dell-PLX. Do not sign in at the console and do not open Remote Desktop. Either of those starts `explorer.exe` and the check is void.

The SSH session has to reach WSL on the `dell-plx-reader` tailnet node. That is sshd in WSL, or Tailscale SSH to that node. The Windows `dell-plx` node is not this session.

## Within 5 minutes

On Dell-PLX, over that SSH session (not a console or RDP session):

```bash
powershell.exe -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"
date -u +%Y-%m-%dT%H:%M:%SZ
```

Write both into the record. The boot time is UTC. The checks that follow use the same `date -u` line so the gap from boot is visible.

From another tailnet machine that has a shell, such as the Mac. Not from Dell-PLX loopback, and not from a phone browser:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
curl -fsS -u "amber:$AMBER_READER_PASSWORD" \
  https://dell-plx-reader.tailf9612a.ts.net/healthz
date -u +%Y-%m-%dT%H:%M:%SZ
curl -fsS https://dell-plx-reader.tailf9612a.ts.net:8443/healthz
```

The first URL is the reader on 8081. The second is ingest on 8082. Paste both bodies and both timestamps into the record.

amber-web is still only on loopback. From the same SSH session on Dell-PLX:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
curl -fsS -u "amber:$AMBER_READER_PASSWORD" http://127.0.0.1:8084/healthz
```

## No interactive user

Stay on the SSH session into WSL. Do not open a Windows desktop.

```bash
query.exe user
tasklist.exe /FI "IMAGENAME eq explorer.exe"
```

With nobody signed in, `query.exe user` prints `No User exists for *` and exits 1. `tasklist.exe` prints `INFO: No tasks are running which match the specified criteria.` Paste both into the record.

If `query.exe` or `tasklist.exe` fails under the S4U session, paste that error into the record. Do not sign in at the desktop to get a substitute answer.

A console or RDP sign-in would list a user and an `explorer.exe`. That means the S4U task was not the only session. The check failed.

## If a check fails

Fix the S4U boot task so the user services start without a desktop session. Restore nothing else yet. Reboot and run this checklist again from "Reboot". Do not treat a later pass as the same boot.

After a pass, if Jeremy wants the auto-sign-in setting back, turn it on and write that in the record. Leave it off if he does not.
