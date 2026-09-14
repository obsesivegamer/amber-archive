# Verification

Build the test container with `docker build -t amber-tests .`, then run
`docker run --rm amber-tests`. The image copies only source and tests, not saved
articles, credentials, or the production database. Tests use temporary storage.

For the FT title change, lint with:
`docker run --rm amber-tests ruff check --select E9,F63,F7,F82 app/extract.py tests/test_ft_titles.py tests/test_e2e_capture.py`.
Compile with `docker run --rm amber-tests python -m compileall -q app tests`.

If Docker is unavailable, use an existing Python environment without installing
system packages. Run from this worktree so pytest imports this branch's app.
Set `AMBER_DATA_DIR` to a temporary directory before tests. Browser tests need
installed Playwright Chromium and permission to bind a localhost fixture server.
Report unavailable container verification explicitly.
