# Architecture

How a request flows, where the vault and security live, and the data conventions. Moved out of `CLAUDE.md` so it loads only when needed.

- Request flow: page/component (`frontend/src/pages`, `components`) -> `api.ts` (`api.<area>.<fn>`,
  the only place that calls `fetch`) -> router (`backend/app/routers/<area>.py`) -> service
  (`backend/app/services/*.py`, the math and rules) -> SQLAlchemy models (`backend/app/models.py`).
  Routers stay thin; put logic in services so tests can call it directly.
- Vault: SQLCipher database under the repo's `data/` folder (or `FINTRACK_HOME/data` when packaged),
  key derived from the master password (Argon2id) and held in memory only while unlocked.
  `security.py` has `Vault` (unlock/lock, hooks `before_lock` = automatic backup,
  `after_unlock` = load Plaid keys + finish updates), sessions, rate limiting and `SecurityMiddleware`.
- Every request: Host allowlist; every non-GET needs header `X-FinTrack: 1`, an allowed Origin and a
  JSON body (multipart only for the two uploads). Session routes use `Depends(get_db)` (`deps.py`),
  which requires the `ft_session` cookie + `X-FinTrack-Session` header; 401 flips the UI to locked.
- Request bodies: `schemas.py`. New bodies subclass `StrictModel3` (unknown fields and NaN/Infinity
  -> 422). PATCH handlers use `model_fields_set` + `non_null(...)` so "missing" differs from "null".
  Validation errors never echo input back (passwords).
- Money: integer **cents** in the database and services (`*_cents`, `utils.to_cents/from_cents`),
  **dollars** (float) in every API request/response. Transaction sign: negative = money out
  (Plaid's sign is flipped in `services/sync.py` `_txn_fields`).
- Months are `"YYYY-MM"` strings everywhere (budgets, reports); dates are ISO `YYYY-MM-DD`.
  `utils.today()` is the local date.
- New simple settings go in the `app_settings` key/value table (`AppSetting`, see
  `services/spending.get_setting/put_setting`, `services/automation._get/_put`) instead of a
  migration. Real schema changes: add `_vN` in `migrations.py`, bump `LATEST` (now 8), update
  `models.py`, add a test like `test_migrations.py`; a safety copy is made before every upgrade.
- Frontend: React 18 + TS + Vite, `HashRouter` (`#/spending`), plain CSS with tokens in
  `styles.css` (+ `styles-a.css`/`styles-b.css`, split by the two agents who built Release 2; same
  for `dev/mockA.ts`/`mockB.ts`). Data hook: `lib/useApi.ts`; app state and the lock phases:
  `state.tsx`; prefs in localStorage via `lib/prefs.ts`.
- Private updates (`backend/app/updates/`, `routers/update.py`) are signed `.ftupdate` files. The
  updater is off in git checkouts (mode `git`/`dev`), so update routes answer 409 there.
- Update sources (`updates/source.py`): `file` (emailed files, the owner's and family installs) or
  `github` (Iron Owl installs: `updates/github.py` checks GitHub Releases once a day while unlocked,
  HTTPS to allowlisted GitHub hosts only, verified with `update_keys.GITHUB_KEYS`). Network rule:
  the app talks only to Plaid and, in github mode, to GitHub; the update check sends no user data.
