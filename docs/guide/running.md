# Running things

Tests, the repo map, the frontend, the mock UI, worktrees and the real app. Moved out of `CLAUDE.md` so it loads only when needed.

- Backend tests (from a worktree too; the venv lives only in the main folder, `<repo>` below):
  `cd backend && "<repo>/backend/.venv/Scripts/python" -m pytest -q -p no:cacheprovider --basetemp <scratch dir>`
  - `--basetemp` must be an empty folder of its own under your scratchpad: pytest deletes it.
  - `pytest.ini` already adds `-q`, so with another `-q` nothing but dots prints (no summary):
    rely on the exit code, or add `-rfE` to list failures.
  - The full suite (about 1,550 tests) takes more than 10 minutes: run the files for the area you changed
    (MAP.md "Tests" lists them per feature), and the full suite in the background before committing.
  - Tests use `FakePlaid` and a temp vault (`tests/conftest.py`); they never need the network.
- Repo map (stdlib only, ~2 s; any Python 3.9+, e.g. the venv's): `python tools/repo_map.py` after
  changing code (routes, files, line numbers all move); `python tools/repo_map.py --check` exits 1
  when stale. `tests/test_repo_map.py` fails until you regenerate, so do it last before committing.
- Frontend (from `frontend/`): `npx tsc --noEmit` (typecheck), `npm run build` (typecheck + Vite
  build into `frontend/dist`), `npm run test:unit` (node tests in `frontend/scripts`).
- UI without the backend: `npm run dev:mock` (Vite on `http://127.0.0.1:5173`; also in
  `.claude/launch.json` as `frontend-mock`). Scenario query params go before the `#`, e.g.
  `http://127.0.0.1:5173/?mock=full&budget=over#/spending`:
  `mock=full|empty|locked|setup|noplaid|envplaid|norecovery|elsewhere|offline|lockout|idle|closed`,
  `latency=ms`, `keys=none|vault|vaultbad|env|envboth|partial`, `home=normal|lots|bankerr|pending`,
  `homeerr=...`, `budget=setup|normal|done|more|less|over|off`, `rec=none|unconfirmed|stale`,
  `update=banner|news|progress|...`, `install=ok|fail|help|migfail|crash`. The full list is in the
  header comments of `frontend/src/dev/mock.ts`, `mockHome.ts`, `mockBudget.ts`, `mockRecovery.ts`,
  `mockPresence.ts`, `mockUpdates.ts` and `mockSettings.ts` (Settings D7: `autolock=env`,
  `pwchanged=0`); Settings backup folders live in `mockA.ts` (`backuperr=1`, `backups=off`,
  `backuprun=fail`, `suggested=none`, `picker=none|cancel`). Mock password: `correct horse battery`. When you add a route,
  add its mock too, or the mock UI breaks.
- The real app: `start.bat` (runs `start.ps1`: venv, pip, frontend build, then `backend/run.py`).
  It refuses to start when `backend/app` or `frontend/src` has uncommitted changes. Packaged
  installs start through `packaging/launcher/launch.pyw` -> `backend/run_packaged.py`.
- Worktree frontend: create `frontend/node_modules` as a junction to the main folder's with PowerShell
  `New-Item -ItemType Junction -Path <worktree>\frontend\node_modules -Target "<repo>\frontend\node_modules"`
  (links made from Git Bash break). Before removing the worktree, remove the junction
  with `cmd /c rmdir <worktree>\frontend\node_modules` (never a recursive delete: it would follow the
  junction). Never `git worktree remove --force`, and never remove a worktree in the same command as a
  commit. `npm install <pkg>` in a worktree replaces the junction with a real folder (the main
  folder's node_modules is untouched and lacks the package until its `start.ps1` runs `npm ci`,
  which it does whenever `package-lock.json` changes); delete that real folder normally afterwards.
- Harmless noise: `LF will be replaced by CRLF` warnings (`core.autocrlf=true`); the starlette
  httpx deprecation warning is filtered in `pytest.ini`.
