# Amber verification map

This directory is the maintained source for verifying Amber's user-facing behavior. Read this index, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch Amber with `.cursor/skills/verify-amber/scripts/launch.ps1` (Windows) or `scripts/launch.sh` (Linux/macOS).
- Instance URL is `http://127.0.0.1:18080` (or `$env:AMBER_VERIFY_PORT` / `AMBER_VERIFY_PORT`) with `AMBER_DATA_DIR` under `.cursor/skills/verify-amber/.scratch/data`.
- Run `scripts/doctor.ps1` or `scripts/doctor.sh` and require exit 0: verify URL, scratch data dir, live PID, homepage 200.
- Never drive `http://127.0.0.1:8080` or write to repo `data/`. That is the user's archive.
- Seed snapshots only through `/import` or `/save`, not by inserting SQLite rows.
- The fixture title is `Amber Verification Bridge`. The body token is `VERIFICATION_TOKEN_AMBER_BRIDGE_2026`.

## Driving conventions

- Start every recipe from the baseline unless its preconditions say otherwise.
- Prefer labels and ids (`#url`, `#file`, `#q`, wordmark `Amber`, buttons `save` / `import` / `search` / `delete`) over CSS position.
- Treat every command as literal.
- Browser actions go through cursor-ide-browser against the verify URL when that MCP is available; otherwise use the available browser or HTTP against the same verify URL.
- HTTP actions hit the same form routes the templates post to.
- After a mutation, confirm from a second surface (browse, search, or files on disk). Do not remove proof artifacts during cleanup.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final screen.
- UI proof includes an ARIA snapshot and a screenshot with the Amber wordmark visible.
- HTTP proof includes status, headers, and a body excerpt.
- Mutation proof includes the snapshot folder and a SQLite row (or their absence after delete).
- Record the feature ID and entry point in `evidence/<feature-id>/meta.txt`.
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with browser and HTTP` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

Keep implementation details out of the map. Name only user paths, stable handles, required state, commands, and observable proof.

## Features

- [Save a URL](./save-url.md) covers the homepage form, bookmarklet `/save?url=`, rejected URLs, already-saved, and the saving poll.
- [Import HTML](./import-html.md) covers uploading a saved page and landing on the new snapshot.
- [Search the archive](./search.md) covers home and `/search` queries, matches, and empty results.
- [Browse and delete](./browse-saved.md) covers `/saved`, the home recent list, and delete.
- [View a snapshot](./view-snapshot.md) covers article, webpage, and screenshot modes and their file routes.
