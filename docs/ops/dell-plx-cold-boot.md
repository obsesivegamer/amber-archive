# Cold boot

This checklist needs Jeremy's yes before anyone reboots Dell-PLX. The change that added it does not reboot the machine.

The existing S4U scheduled task is what starts the session at boot. This checklist does not replace that task.

## Before the reboot

Turn off Windows' "use my sign-in info to finish setting up after an update".

Settings, Accounts, Sign-in options. The switch is "Use my sign-in info to automatically finish setting up my device after an update or restart". Set it off.

## Reboot

Reboot Dell-PLX. Do not sign in at the console.

## Within 5 minutes

From another tailnet machine:

```bash
curl -fsS -u "amber:$AMBER_READER_PASSWORD" \
  https://dell-plx-reader.tailf9612a.ts.net/healthz
curl -fsS http://127.0.0.1:8082/healthz
```

The second command is on Dell-PLX itself, over loopback. amber-web, still on loopback until cutover:

```bash
curl -fsS -u "amber:$AMBER_READER_PASSWORD" http://127.0.0.1:8084/healthz
```

Reader is 8081 behind the tailnet name. Ingest is 8082. Import is 8083. amber-web is 8084.

## No interactive user

On Dell-PLX:

```bash
query user
```

The output is empty. `explorer.exe` is not running:

```bash
tasklist /FI "IMAGENAME eq explorer.exe"
```

That lists no explorer.exe. The S4U task is the session that started the user services. A person signed in at the console would show a user in `query user` and an `explorer.exe`.
