# FinTrack — Architecture & API Contract

Local-only personal finance tracker: bank, HSA, 401(k)/retirement, loans, credit cards.
Plaid for linked accounts (Transactions, Investments, Liabilities), manual entry for anything else.

## Layout

```
backend/            FastAPI app (Python 3.11)
  app/
    main.py         app factory, middleware, static serving of frontend/dist
    config.py       settings from env / .env
    security.py     KDF, key handling, sessions, CSRF/host checks, rate limiting
    db.py           SQLCipher engine lifecycle (open on unlock, dispose on lock)
    models.py       SQLAlchemy 2.0 models
    schemas.py      Pydantic v2 request/response models
    plaid_client.py thin wrapper around plaid-python
    services/       sync.py, networth.py, categorize.py
    routers/        auth.py, accounts.py, summary.py, transactions.py, holdings.py, plaid.py
  tests/            pytest; Plaid is always mocked
  requirements.txt
frontend/           React 18 + TypeScript + Vite
data/               (gitignored) fintrack.db (SQLCipher-encrypted), keyfile.json (KDF salt+params and the recovery sheet's sealed key, not secret)
.env / .env.example PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV=sandbox, AUTO_LOCK_MINUTES=15, HOST=127.0.0.1, PORT=8000
```

In development Vite runs on :5173 and proxies `/api` → `http://127.0.0.1:8000`.
In "production" (normal use) the backend serves `frontend/dist` at `/`, so everything is one origin at `http://127.0.0.1:8000`.

## Security model

1. **Network**: uvicorn binds `127.0.0.1` only. A Host-header allowlist (`127.0.0.1:PORT`, `localhost:PORT`, plus `:5173` variants in dev) rejects everything else with 400 — this stops DNS-rebinding attacks.
2. **Encryption at rest**: the whole database is SQLCipher (`sqlcipher3-wheels`, AES-256). The DB key is **derived from the master password** via Argon2id (`argon2-cffi` low-level `hash_secret_raw`, type ID, time_cost=3, memory_cost=65536 KiB, parallelism=4, hash_len=32, 16-byte random salt). It is passed as a raw key: `PRAGMA key = "x'<64 hex>'"`. The key is never written to disk. Set `PRAGMA cipher_log_level = NONE` (or equivalent) so wrong-key attempts don't spam stderr.
   - `data/keyfile.json` = `{"version":1,"kdf":"argon2id","salt":"<b64>","time_cost":3,"memory_cost":65536,"parallelism":4}`, plus an optional `"recovery"` block (the recovery sheet, see "Recovery sheet" at the end; older builds ignore it). It is not secret.
   - Password verification = successfully opening the DB (`SELECT count(*) FROM sqlite_master`). There is no separate password hash.
   - **Change password**: derive the new key with a *new* salt, re-seal the new key to the same recovery sheet, write `keyfile.json.new`, `PRAGMA rekey`, then atomically replace `keyfile.json`. On unlock (and recover), if `keyfile.json.new` exists, try it too (crash recovery) and finalize whichever works.
   - **Recovery sheet**: the key is also sealed to the public key of a printed 36-digit code, so a forgotten password can be replaced (`POST /api/auth/recover`). The code itself is never stored.
   - Plaid access tokens live inside the encrypted DB and are **never** returned by any API response or written to logs.
   - Release 3.4: the Plaid secret may also be stored inside the encrypted DB (Settings → Bank connection). It is write-only: never returned (only its last 4 characters), never logged, and held in memory only while unlocked. See Release 3.4.
3. **Locked vs unlocked**: while locked, the process holds no key and no DB engine. Unlock derives the key, opens the engine, and creates a session. Lock (explicit, idle timeout, or process exit) disposes the engine and drops the key reference.
   - Auto-lock after `AUTO_LOCK_MINUTES` (default 15) with no authenticated request. The server enforces this; the frontend only mirrors it.
4. **Sessions**: single-user. On setup/unlock the server creates a 32-byte `secrets.token_urlsafe` session id and sets cookie `ft_session` with `HttpOnly; SameSite=Strict; Path=/` (not `Secure`, because it's plain-HTTP localhost). Sessions are in memory only, so a server restart means locked. There is one active session at a time; a new unlock replaces the old one (the replaced window then gets 401 `moved`, see "App window states").
   - **Session token (cookie is not enough).** Browsers send cookies to every port on 127.0.0.1, so another local server could receive `ft_session`. Setup, unlock and change-password therefore also return a second random `session_token` in the JSON body. The frontend keeps it in `sessionStorage`, which is isolated per origin (port included) and per tab, and sends it as `X-FinTrack-Session` on every request. A session is valid only if both the cookie and the header match (constant-time). A new tab must unlock.
5. **CSRF**: every non-GET `/api` request must carry the header `X-FinTrack: 1`, and its `Origin` (if present) must be in the allowlist. Otherwise → 403. SameSite=Strict adds defense in depth.
6. **Brute force**: unlock/setup/change-password failures (and every other password re-check, and wrong recovery sheets) share one exponential backoff (in memory, and in `data/limiter.json` so a restart doesn't reset it; see "App window states"): after 5 consecutive failures, lock out for 30s, doubling on each further failure (max 15 min). A wrong try answers 401 with `tries_left` (tries before a wait starts); a lockout is 429 `{"code":"rate_limited","retry_after":n}` + `Retry-After`. `GET /api/auth/status` also reports `retry_after` and `tries_left`, so the unlock screen can show a wait after a reload.
7. **Headers** (on every response): `Content-Security-Policy: default-src 'self'; script-src 'self' https://cdn.plaid.com; frame-src https://cdn.plaid.com; connect-src 'self' https://*.plaid.com; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. `/api/*` responses also get `Cache-Control: no-store`.
8. **Password policy**: minimum 12 characters. Nobody can reset the password; the only way back in without it is the **recovery sheet** made at setup (or later in Settings). Losing both the password and the sheet means lost data, and the UI says so at setup. Anyone with the sheet *and* the computer can set a new password (by design); keep the sheet away from the computer.
9. **Logging**: never log request bodies, tokens, the Plaid secret (whether from `.env` or stored inside the encrypted vault), or account numbers. Plaid errors are reduced to `error_type`/`error_code`/`error_message` before logging or returning; the key endpoints (Release 3.4) never return Plaid's `error_message` at all.
10. **Docs**: FastAPI `/docs`, `/redoc` and `/openapi.json` are disabled unless `DEBUG=1`.

## Data model (SQLAlchemy 2.0, all money stored as integer cents)

- **plaid_items**: id PK, plaid_item_id (unique), access_token, institution_id, institution_name, kind (`bank|investment|loan`), transactions_cursor (nullable), status (`ok|login_required|error`), error_code (nullable), last_synced_at (nullable), created_at
- **accounts**: id PK, item_id FK nullable (NULL = manual), plaid_account_id (unique, nullable), source (`plaid|manual`), name, official_name, mask, institution_name, category (`bank|hsa|retirement|investment|loan|credit|other`), plaid_type, plaid_subtype, current_balance_cents, available_balance_cents (nullable), currency (default USD), interest_rate (float %, nullable), minimum_payment_cents (nullable), next_payment_due (date, nullable), notes (nullable), hidden (bool), updated_at, created_at
- **balance_snapshots**: id, account_id FK (cascade), date, balance_cents; UNIQUE(account_id, date) — upserted on every sync and on every manual balance change
- **transactions**: id, account_id FK (cascade), plaid_transaction_id (unique), date, name, merchant_name, amount_cents (**our sign convention: negative = money out, positive = money in**; Plaid's is the opposite, so flip it), category (Plaid personal_finance_category.primary, nullable), pending (bool)
- **holdings**: id, account_id FK (cascade), security_id, name, ticker (nullable), quantity (float), price (float, nullable), value_cents, cost_basis_cents (nullable); replaced wholesale on each investments sync

**Category mapping** from Plaid (type, subtype):
- depository + hsa → `hsa`; investment + hsa → `hsa`
- depository (other) → `bank`
- investment + any of 401k, 403b, 457b, ira, roth, roth 401k, sep ira, simple ira, pension, retirement, keogh, 401a, thrift savings plan, sarsep, non-taxable brokerage account → `retirement`
- investment (other) → `investment`
- loan → `loan`
- credit → `credit`
- otherwise → `other`

**Liability categories** (subtracted from net worth): `loan`, `credit`. Balances are stored as positive numbers and the math subtracts them. Hidden accounts are excluded from the summary and history.

## API contract

All JSON. Money in responses is a **number in dollars** (cents / 100). Dates are ISO `YYYY-MM-DD`; datetimes are ISO 8601 UTC.
Errors: `{"detail": "<message>"}` with the right status. If a route needs the DB and the app is locked → **401** `{"detail":"locked"}`; if the caller's session was taken over by another FinTrack window (an unlock or "Open here" there) → **401** `{"detail":"moved"}` (until the next lock). The frontend treats any 401 as "show the unlock screen" (except password and recovery-sheet attempts, whose `detail` is never `"locked"`). Vault errors (auth, recovery, backup/restore, re-auth) also carry a machine-readable `code` (`wrong_password`, `rate_limited`, `weak_password`, `password_required`, `already_initialized`, `not_initialized`, `bad_backup`, `locked`, `pre_migration_copy_failed`, and the recovery codes below); FastAPI validation errors (422) have only `detail`.

### Auth (no session required)
| Method | Path | Body | Response |
|---|---|---|---|
| GET | /api/auth/status | – (optional `X-FinTrack-Tab`) | `{initialized: bool, unlocked: bool, auto_lock_minutes: int, recovery: {available: bool, sheet: "482-913"|null}, open_elsewhere: bool, lock_reason: "idle"|"closed"|"manual"|"moved"|"shutdown"|null, retry_after: int|null, tries_left: int, support_contact: str|null}` (unlocked = this request's cookie + `X-FinTrack-Session` header are a valid session; `support_contact` = `SUPPORT_CONTACT`, null when empty; `open_elsewhere`/`lock_reason`: see "App window states"). Marks the tab seen and cancels its pending close; never activity |
| POST | /api/auth/setup | `{password}` | 200 `{ok:true, session_token, recovery: {sheet, created_at, groups: [6 × "739151"]}}` + cookie. 409 if already initialized. 422 if password < 12 chars |
| POST | /api/auth/unlock | `{password}` | 200 `{ok:true, session_token}` + cookie. 401 `{"detail":"invalid password","code":"wrong_password","tries_left":n}`. 429 `{"detail":"too many attempts","code":"rate_limited","retry_after":n}` |
| POST | /api/auth/lock | none, `{}` or `{"reason":"idle"|"manual"}` (default manual; others 422) | 200 `{ok:true}`, clears cookie. A caller whose session moved to another window gets 200 `{ok:true}` and nothing happens (no lock, cookie kept) |
| POST | /api/auth/change-password | `{current_password, new_password}` (session required) | 200 `{ok:true, session_token}` (session rotated). 401 if current is wrong |
| POST | /api/auth/recover/check | `{code}` | 200 `{ok:true, sheet}`; errors: see "Recovery sheet" |
| POST | /api/auth/recover | `{code, new_password}` | 200 `{ok:true, session_token}` + cookie (like unlock); errors: see "Recovery sheet" |

### Accounts (session required)
`Account` = `{id, source:"plaid"|"manual", item_id:int|null, name, official_name:str|null, mask:str|null, institution_name:str|null, category, plaid_type:str|null, plaid_subtype:str|null, current_balance:number, available_balance:number|null, currency, interest_rate:number|null, minimum_payment:number|null, next_payment_due:str|null, notes:str|null, hidden:bool, is_liability:bool, updated_at}`

| Method | Path | Body | Response |
|---|---|---|---|
| GET | /api/accounts | – | `Account[]` (includes hidden; ordered by category, then name) |
| POST | /api/accounts | `{name, category, current_balance, institution_name?, interest_rate?, minimum_payment?, next_payment_due?, notes?}` | 201 `Account` (source=manual, also writes today's snapshot) |
| PATCH | /api/accounts/{id} | any of `{name, category, hidden, notes, interest_rate, minimum_payment, next_payment_due, current_balance}` — `current_balance` only allowed for manual (400 otherwise) | `Account` |
| DELETE | /api/accounts/{id} | – | 204. Manual accounts only (400 for Plaid — remove the item instead) |
| GET | /api/accounts/{id}/history?days=365 | – | `[{date, balance}]` |

### Summary (session required)
| GET | /api/summary | – | `{net_worth, total_assets, total_liabilities, by_category: {bank, hsa, retirement, investment, loan, credit, other}: number, account_count, last_synced_at: str|null, items_needing_attention: int}` |
| GET | /api/networth/history?days=365 | – | `[{date, assets, liabilities, net_worth}]`, one point per day from the earliest snapshot (clamped to `days`) to today, carrying each account's last known balance forward |

### Transactions & holdings (session required)
| GET | /api/transactions?account_id=&search=&start=&end=&limit=50&offset=0 | – | `{items: [{id, account_id, account_name, date, name, merchant_name, amount, category, pending}], total}` ordered by date desc. limit max 500. search = case-insensitive match on name/merchant |
| GET | /api/holdings?account_id= | – | `[{id, account_id, account_name, name, ticker, quantity, price, value, cost_basis}]` |

### Plaid (session required)
`Item` = `{id, institution_name, kind, status, error_code, last_synced_at, account_count}`

| Method | Path | Body | Response |
|---|---|---|---|
| GET | /api/plaid/status | – | `{configured: bool, env: "sandbox"|"production", source: "env"|"vault"|"none"}` (`source` added in Release 3.4: where the active keys come from) |
| POST | /api/plaid/link-token | `{kind: "bank"|"investment"|"loan", item_id?: int}` | `{link_token}`. kind picks the **required** product (bank=transactions, investment=investments, loan=liabilities); the other two are sent as `optional_products`, so one link imports every account at an institution. With `item_id` → update mode (access_token, no products). 503 if Plaid is not configured |
| POST | /api/plaid/exchange | `{public_token, kind}` | 201 `Item` — exchanges the token, stores the item, and runs an immediate sync of that item. **409** if a different item at the same institution already has any of the same accounts (matched on name + mask + subtype); the new item is removed at Plaid |
| GET | /api/plaid/items | – | `Item[]` |
| POST | /api/plaid/sync | `{item_id?: int}` | `{results: [{item_id, institution_name, ok, error_code|null, accounts, transactions_added, transactions_modified, transactions_removed, holdings}]}` — syncs one item or all |
| DELETE | /api/plaid/items/{id} | – | 204 — calls /item/remove at Plaid (best-effort), deletes the item and its accounts (cascade) |

**Sync per item**: `/accounts/get` → upsert accounts + today's snapshot. `/item/get` tells which products the item actually has; only those endpoints are called (calling an un-enabled product's endpoint makes Plaid add and bill it). If Plaid reports none, fall back to the item's kind. If the item has transactions (or a stored cursor), `/transactions/sync` loops on `has_more` with the stored cursor (added/modified/removed; on `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` restart from the original cursor). Investment → `/investments/holdings/get` (replace holdings; account balances come from there too). Loan → `/liabilities/get` (fill interest_rate, minimum_payment, next_payment_due on the matching accounts: student loans/mortgage use their fields, credit uses aprs[0].apr_percentage and minimum_payment_amount). `ITEM_LOGIN_REQUIRED` → status=`login_required`. Other Plaid errors → status=`error` + error_code. A failure on one item must not abort the others. Products the institution doesn't support (`PRODUCTS_NOT_SUPPORTED`, `NO_INVESTMENT_ACCOUNTS`, `NO_LIABILITY_ACCOUNTS`, etc.) are skipped, not errors.

## Frontend

React 18 + TS + Vite, `react-plaid-link`, `recharts`, `react-router-dom`. No UI framework — hand-written CSS with CSS variables, light and dark (prefers-color-scheme), responsive down to phone width. It should look like a calm, dense, trustworthy finance dashboard, not a toy.

- A single `api.ts` fetch wrapper: `credentials: "same-origin"`, adds `X-FinTrack: 1` and a JSON content-type, throws a typed error, and any 401 triggers a global "locked" state.
- **Screens**: Setup (first run: password + confirm, strength hint, big "no recovery" warning) · Unlock · Dashboard (net-worth hero number, assets/liabilities, category cards, net-worth-over-time area chart with 30d/90d/1y/all, accounts needing attention banner) · Accounts (grouped by category, with inline balance edit for manual accounts, hide toggle, a per-account balance history drawer or panel) · Transactions (table with search, account filter, date range, pagination) · Investments (holdings table grouped by account with totals and gain/loss) · Loans (loan + credit accounts with rate, min payment, next due date, and a payoff estimate) · Settings (Plaid connections: add bank / add 401k·HSA·investment / add loan buttons; per-item status, re-authenticate (update mode), sync, remove with confirm; change password; lock now).
- Nav includes a "Sync" button and a "Lock" button. After unlock, auto-sync if `last_synced_at` is null or older than 12h and Plaid is configured.
- A client-side idle timer mirrors `auto_lock_minutes` and calls `/api/auth/lock`.
- Nothing sensitive goes in localStorage (at most UI preferences like the chart range).
- If Plaid isn't configured, the Settings page shows setup instructions instead of Link buttons, and the manual-account flow still works fully.

---

# Release 2: budgets, recurring + forecast, goals, rules + alerts, add-account flow, extras

The design handoff (`.dc.html` prototypes and a README) isn't in the repo. This section is the authoritative **API contract**. Frontend types for every shape below are in `frontend/src/api.ts` (section "Release 2"), and the backend and frontend must match them exactly.

Unless a route says otherwise:
- all routes require a session;
- money is dollars (number);
- months are `YYYY-MM`;
- "today" is the machine's local date (`utils.today()`).

## Schema migrations
The app has no migrations yet, so this release adds them. Use `PRAGMA user_version` and run migrations inside `Database.open()` right after the key is applied (on setup and on unlock), each migration in a transaction.

- **v1** is the Release 1 schema. It applies when a DB has `user_version = 0` and already has tables: just stamp it as v1. A fresh DB gets the latest schema from `create_all` and is stamped at the latest version.
- **v2** (this release):
  - create the new tables below;
  - `ALTER TABLE transactions ADD` `plaid_category TEXT`, `category_source TEXT NOT NULL DEFAULT 'plaid'`, `rule_id INTEGER`, `notes TEXT`, `is_transfer BOOLEAN NOT NULL DEFAULT 0`, and `transfer_source TEXT` (NULL, `rule` or `user`);
  - backfill `plaid_category = category`;
  - `ALTER TABLE plaid_items ADD excluded_account_ids TEXT NOT NULL DEFAULT '[]'`;
  - seed `categories` and `alert_settings`.
- Tests must cover upgrading a real v1 DB: build the Release 1 schema, set `user_version = 0`, insert rows, then migrate.

## New and changed tables (money in integer cents)

**categories**: `id` TEXT PK, `name`, `hue` INT 0-359, `kind` (`spending|income|transfer|fixed`), `custom` BOOL, `hidden` BOOL, `position` INT. Custom ids are `c_<n>`.

Seeded with the Plaid PFC primaries:

| id | name | hue | kind |
|---|---|---|---|
| FOOD_AND_DRINK | Food and drink | 25 | spending |
| TRANSPORTATION | Transportation | 230 | spending |
| GENERAL_MERCHANDISE | Shopping | 300 | spending |
| ENTERTAINMENT | Entertainment | 340 | spending |
| RENT_AND_UTILITIES | Rent and utilities | 85 | spending |
| MEDICAL | Medical | 160 | spending |
| TRAVEL | Travel | 200 | spending |
| PERSONAL_CARE | Personal care | 120 | spending |
| GENERAL_SERVICES | Services | 260 | spending |
| HOME_IMPROVEMENT | Home improvement | 50 | spending |
| BANK_FEES | Bank fees | 10 | spending |
| GOVERNMENT_AND_NON_PROFIT | Taxes and donations | 280 | spending |
| OTHER | Other | 0 | spending |
| INCOME | Income | 150 | income |
| TRANSFER_IN | Transfer in | 240 | transfer |
| TRANSFER_OUT | Transfer out | 240 | transfer |
| LOAN_PAYMENTS | Loan payments | 55 | fixed |

If sync sees an unknown Plaid primary, it inserts it as `spending`, with a title-cased name and hue `fnv1a(id) % 360`.

**transactions** (changed):
- `category` now holds the **effective** category id.
- `plaid_category` holds Plaid's raw primary.
- `category_source` is `plaid|rule|user|user_bulk` (`user_bulk` since Release 3.3: set by hand on several transactions at once). `user` and `user_bulk` are both **hand-set**.
- New columns: `rule_id` (nullable), `notes`, `is_transfer`, `transfer_source`.
- A null category counts as `OTHER`.
- Precedence: hand-set (`user`/`user_bulk`) > first matching enabled rule > `plaid_category` > `OTHER`.
- Sync writes `plaid_category`, then re-resolves the category unless it's hand-set (`category_source` in `user`, `user_bulk`).

**plaid_items** (changed):
- `excluded_account_ids` is a JSON list of Plaid account_ids the user chose not to import.
- `status` gains `pending` (exchanged, awaiting account selection).

**budget_months**: `month` TEXT PK, `pool_cents`.

**budgets**: `id`, `month`, `category` (FK categories.id ON DELETE CASCADE), `limit_cents`. UNIQUE(month, category).

**recurring_items**:
- `id`, `name`, `merchant_key` (normalized lowercase merchant_name or cleaned name), `account_id` (FK SET NULL);
- `amount_cents` (signed: + in, - out);
- `cadence` (`weekly|biweekly|semimonthly|monthly|quarterly|yearly`), `next_date`;
- `status` (`suggested|active|dismissed`), `include_in_forecast` BOOL (default true);
- `source` (`detected|manual`), `last_seen_date` (nullable), `created_at`.

**goals**:
- `id`, `name`, `kind` (`save|debt`), `account_id` (FK SET NULL, nullable);
- `target_cents` (save), `start_cents` (debt starting balance), `current_cents` (used only when no account is linked);
- `monthly_cents`, `apr` (float %, nullable), `target_date` (nullable), `hue`, `created_at`.

**rules**:
- `id`, `position`, `field` (`any|merchant|name`), `op` (`contains|is`), `text`;
- `amount_op` (`gt|lt|null`) and `amount_cents` (nullable), compared against the **absolute** transaction amount;
- `action` (`category|transfer`), `category` (FK; required when action=category), `enabled`.

**alert_settings**: `key` PK, `enabled`, `value` (float, nullable). Seeds, all enabled:
- low 2000 (usd)
- big 250 (usd)
- budget 90 (percent)
- due 3 (days)
- newrec, price, conn (no value)

**alert_events**: `id`, `key`, `severity` (`warn|neg|accent`), `title`, `body`, `created_at`, `read` BOOL, `dedupe_key` UNIQUE.

**app_settings**: `key` PK, `value` TEXT. Used for `forecast_account_id`.

## Spending definitions (use these everywhere)
- A **spending transaction** has all of the following (pending transactions count):
  - an account whose category is `bank|credit|hsa|other` and which is not hidden;
  - `is_transfer = false`;
  - an effective category of kind `spending`.
- `spent` for a category and month = `max(0, -sum(amount))`, so refunds net out.
- **Income** for a month = sum of amount > 0 over kind `income` transactions, with the same account filter.
- **Fixed** = `max(0, -sum(amount))` over kind `fixed`.
- **Pace**: for the current month, `day_of_month / days_in_month`; for past months, 1.

## Budgets and spending

**`GET /api/budgets?month=`** (default: current month) returns `BudgetMonth`.
- If the month has no saved budget, return the latest earlier saved month's pool and limits with `inherited: true`. If there is none, return pool 0 with no limits.
- `categories`: every budgeted category (limit > 0), with its limit and spent.
- `unbudgeted`: spending categories with spend but no budget.
- `fixed`: fixed-kind categories with spend.
- `available`: every non-hidden spending category.
- `earliest_month`: the earliest month with any transaction or budget, or null.

**`PUT /api/budgets/{month}`** with `{pool, limits: {category_id: number}}` returns `BudgetMonth`.
- Limits of 0 delete the row.
- It returns 422 when:
  - any value is < 0;
  - a category is unknown or not spending-kind;
  - the month is after the current month;
  - **the limits add up to more than the pool**. The detail for this one is "Budgets add up to $X, more than your spending money of $Y."

**`GET /api/spending/review?month=`** returns `SpendingReview`:
- `income`, `fixed`, `budgeted_spending`, `other_spending`;
- `left_over` = income - fixed - budgeted - other;
- `left_over_pct`: share of income, or null if there's no income;
- `compare`: previous month's total spending, plus `delta_pct`;
- `biggest`: the top spending category;
- `most_visited`: the merchant with the most spending transactions, with count and spent;
- `history`: the last 6 months including this one, each `{month, spent, assigned}`. `spent` is all spending; `assigned` is the sum of limits in effect, counting inherited ones.

## Recurring and forecast

**Detection** (`services/recurring.py`) runs after every sync and via `POST /api/recurring/detect`.
- Input: the last 400 days of non-pending, non-transfer transactions on non-hidden accounts, grouped by (account_id, merchant_key, sign).
- A group qualifies when it has at least 3 occurrences (2 for quarterly or yearly) and its median interval fits one cadence band below, with at least 70% of intervals in that band:

  | cadence | interval |
  |---|---|
  | weekly | 6-8 days |
  | biweekly | 13-15 days |
  | semimonthly | 14-17 days, on 2 distinct days of the month |
  | monthly | 27-33 days |
  | quarterly | 85-97 days |
  | yearly | 355-375 days |

- Amounts must be within 20% of the median. The item's amount is the most recent occurrence.
- `next_date` = last occurrence + cadence, rolled forward until it's today or later.
- A new group becomes a `suggested` item.
- An existing item (any status) with the same (account_id, merchant_key) is updated instead (`last_seen_date`, `next_date` rolled forward), never duplicated. `dismissed` items stay dismissed.

**Recurring routes**:
- `GET /api/recurring` returns `RecurringItem[]` for all statuses, ordered by status, then `next_date`.
- `POST /api/recurring` with `{name, amount, cadence, next_date, account_id?}` creates a manual, active item (201).
- `PATCH /api/recurring/{id}` accepts any of `{name, amount, cadence, next_date, account_id, status, include_in_forecast}`.
- `DELETE /api/recurring/{id}` returns 204.
- `POST /api/recurring/detect` returns `{suggested: n_new}`.

**`GET /api/forecast?end=YYYY-MM-DD`** (default: last day of next month) returns `Forecast`.
- **Forecast account**: `app_settings.forecast_account_id` if it still exists. Otherwise, the non-hidden depository checking account with the largest balance. If there is none, return `account: null` with no events.
- `balance`: that account's current balance.
- `threshold`: the low-alert value.
- `daily_spend`: average daily outflow on that account over the last 90 days, rounded to cents. It excludes transfers, fixed and income kinds, and any transaction whose (account, merchant_key) matches an active recurring item.
- `events`: every **active** recurring item whose account is the forecast account (or null) or a credit account, expanded from tomorrow through `end` by its cadence. Semimonthly means the 1st and 15th, or the item's two observed days if known. Each event has:
  - `kind`: `in` (amount > 0), `out`, or `card` (the item's account is credit, so it doesn't move checking);
  - `included` = `include_in_forecast`.
- The client computes the projection (`project()`): for each day from tomorrow, subtract `daily_spend` if enabled, then add every included non-card event.

**`PUT /api/forecast/account`** with `{account_id}` returns `Forecast`. It returns 422 unless the account is depository.

## Goals

**Routes**: `GET /api/goals` returns `Goal[]`. `POST` (201) and `PATCH /{id}` return `Goal`. `DELETE /{id}` returns 204.

**`current`**: the linked account's balance (positive, including for liabilities); if no account is linked, `current_cents`.

**Savings goals (`save`)**:
- `progress = current / target`
- `remaining = max(0, target - current)`
- `months_to_go = ceil(remaining / monthly)`, or null if monthly <= 0 and remaining > 0
- the goal is done when remaining = 0

**Debt goals (`debt`)**: current is the balance owed. `apr` falls back to the account's `interest_rate`, then to 0.
- `progress = (start - current) / start`
- `remaining = current`
- `r = apr / 100 / 12`
- `months_to_go = ceil(-ln(1 - r*B/P) / ln(1 + r))`; `ceil(B/P)` when r = 0; null if P <= B*r

**Derived fields**:
- `projected_date`: the first of the month `months_to_go` months after the current month, or null if never.
- `required_monthly`: the monthly amount needed to finish by `target_date`, or null without a target date. With `n` = whole months from the current month to the target month (at least 1):
  - savings: `remaining / n`
  - debt: `B*r / (1 - (1+r)^-n)`, or `B/n` when r = 0
- `status`:
  - `done` if remaining = 0;
  - `on_track` if there's no target_date;
  - `behind` if projected is null or after target_date;
  - `ahead` if projected is at least 1 month before target_date;
  - `on_track` otherwise.

**`GET /api/goals/suggestions`** returns:
- `emergency_fund`: 3 times the average monthly (spending + fixed) of the last 3 complete months, rounded up to $100;
- `debts`: `[{account_id, name, balance, apr, minimum_payment}]` for non-hidden loan and credit accounts without a goal.

## Rules

**Routes** (types `Rule` and `RuleInput`):
- `GET /api/rules` returns rules ordered by position, each with `matches` (count of transactions whose `rule_id` is that rule).
- `POST` (201, appended last), `PATCH /{id}`, `DELETE /{id}` (204).
- `POST /api/rules/reorder` with `{ids}`, which must be a permutation of all rule ids.
- `POST /api/rules/preview` with a `RuleInput` returns `{matches: n}`: how many transactions it would match, ignoring other rules.

**Matching** is case-insensitive:
- `any` checks name or merchant_name;
- `contains` is a substring match;
- `is` is an exact match after trimming;
- the optional amount condition applies to |amount|.

**Actions**:
- `category` sets the category.
- `transfer` sets `is_transfer = true` with `transfer_source='rule'`. The category is left alone (it falls back to Plaid's).

**Applying rules**: after any rule change and after every sync, re-apply rules in one transaction to every transaction whose category isn't hand-set (`category_source` not in `user`, `user_bulk`).
- The first enabled matching rule wins.
- With no match: reset to `plaid_category`, set `rule_id` to null, and clear `is_transfer` if `transfer_source == 'rule'`.
- Never touch `is_transfer` when `transfer_source == 'user'`.

## Categories and transaction edits

**Category routes**:
- `GET /api/categories` returns `Category[]`, ordered by kind, then position.
- `POST` with `{name, hue?, kind}` (201). Returns 409 if the name already exists, case-insensitive.
- `PATCH /{id}` accepts any of `{name, hue, kind, hidden}`.
- `DELETE /{id}` works on custom categories only (400 otherwise) and returns 409 if a rule uses the category. Its transactions revert to automatic, and its budgets are deleted.

**`PATCH /api/transactions/{id}`** accepts any of `{category: string|null, notes: string|null, is_transfer: bool}` and returns `Transaction`.
- `category` sets `category_source='user'` and `rule_id=null`; `null` resets to automatic and re-applies rules. (Several at once: `PATCH /api/transactions`, Release 3.3, which sets `user_bulk`.)
- `is_transfer` sets `transfer_source='user'`.

**`GET /api/transactions` changes**:
- It gains a `category=` filter (effective id).
- `search` also matches `notes`.
- Items gain `category_name`, `category_hue`, `plaid_category`, `category_source`, `rule_id`, `notes`, `is_transfer`.

**`GET /api/transactions/export.csv`** takes the same filters as the list, with no limit.
- It returns `text/csv; charset=utf-8` with `Content-Disposition: attachment; filename="fintrack-transactions-<today>.csv"`.
- Columns: date, account, name, merchant, category, amount, pending, transfer, notes.
- **CSV-injection guard**: text cells that start with `=`, `+`, `-`, `@`, tab or CR get a leading apostrophe. Numeric cells are written as plain numbers.

## Alerts

**Routes**:
- `GET /api/alerts/settings` returns `AlertSetting[]` in a fixed order: low, big, budget, due, newrec, price, conn. Each has `last`: `{date, text}` from the newest event with that key, or null.
- `PUT /api/alerts/settings/{key}` with `{enabled?, value?}`. For valued keys, `value` must be > 0, and a percent must be <= 100.
- `GET /api/alerts/events?limit=50`, newest first.
- `POST /api/alerts/events/read` marks all events read.
- `DELETE /api/alerts/events` clears them all (204).
- `GET /api/summary` gains `unread_alerts`.

**Evaluation** (`services/alerts.py: evaluate(session, today, new_transaction_ids=())`) runs after every sync, after a manual account's creation or balance change, and after unlock. Each event is created once per `dedupe_key`.

| key | severity | fires when | dedupe_key |
|---|---|---|---|
| low | neg | forecast account balance < value | `low:<acct>:<date>` |
| big | warn | each newly added transaction with amount < -value | `big:<txn id>` |
| budget | warn | current month, spent >= value% of limit | `budget:<month>:<cat>` |
| due | accent | a non-hidden liability's `next_payment_due` is within [today, today+value] | `due:<acct>:<due date>` |
| newrec | accent | detection created a suggested item | `newrec:<item id>` |
| price | warn | for an active item, the latest matching transaction differs from the item amount by more than 1% and more than $0.50 (also update the item's amount) | `price:<item>:<txn>` |
| conn | neg | an item's status changed to `login_required` or `error` | `conn:<item>:<status>:<date>` |

Titles and bodies are short, plain sentences, for example "Chase Checking is below $2,000" / "Balance $1,842.10".

## Plaid: choose which accounts to import (changes Release 1)
- **`POST /api/plaid/exchange`** still takes `{public_token, kind}` and keeps the 409 duplicate rule, but it no longer syncs.
  - It stores the item with status `pending` and returns 201 `{item: Item, accounts: DiscoveredAccount[]}`.
- **`GET /api/plaid/items/{id}/accounts`** returns `DiscoveredAccount[]` live from Plaid, each with `imported`.
- **`POST /api/plaid/items/{id}/import`** with `{plaid_account_ids: string[]}` (at least one) returns `{item: Item, result: SyncItemResult}`. In order, it:
  1. sets `excluded_account_ids` to the rest;
  2. sets status `ok`;
  3. **deletes local Account rows for newly excluded accounts**;
  4. runs sync.
  The same call serves "Manage accounts" later.
- Sync skips excluded accounts entirely (no account rows, transactions or holdings) and skips `pending` items.
- `DELETE` on a pending item works as before.

## Backup and restore

**`POST /api/backup`** with `{password}`:
- The password must equal the vault password. Re-derive it and compare in constant time; attempts count toward the brute-force limiter.
- It returns `application/octet-stream` with `Content-Disposition: attachment; filename="fintrack-backup-<today>.ftbackup"`.
- The file is a zip containing:
  - `fintrack.db`: a consistent **encrypted** copy made with `VACUUM INTO` or the backup API (verify that it isn't plaintext and that it opens with the same key);
  - `keyfile.json`;
  - `manifest.json` with `{format: 1, created_at, schema_version, recovery_sheet}` (`recovery_sheet`: the number of the sheet whose block is in the keyfile, or null).

**`POST /api/restore`** is `multipart/form-data` with `file` and `password`, capped at 200 MB.
- **Without a session, it's allowed only when the vault isn't initialized.** Otherwise it requires a session **and** a `current_password` form field matching the current vault (422 if missing, 401 "Your current Iron Owl password is incorrect." if wrong; counts toward the limiter). A wrong backup password is 401 "That password doesn't open this backup."
- It first validates in a temp dir:
  - the zip is well formed, with no path traversal;
  - the keyfile passes the existing bounds checks;
  - the DB opens with the password.
- Then, in order, it:
  1. locks;
  2. moves the current `data/` files to `data/pre-restore-<timestamp>/`;
  3. installs the backup;
  4. runs migrations;
  5. unlocks with the password.
- It returns `{ok, session_token}` and sets the cookie. A bad file gives 400. A wrong password gives 401 and counts toward the limiter.
- The CSRF middleware must accept multipart **only** on this route, and still require `X-FinTrack: 1` and the Origin check.
- Add the `python-multipart` dependency.

**Previous vaults** (the `data/pre-restore-*` copies a restore sets aside; each still opens with its old password):
- `GET /api/backup/previous` returns `PreviousVault[]` (`{id, created_at (local, no TZ), size_bytes}`), newest first.
- `POST /api/backup/previous/{id}/delete` with `{password}` (the current vault password, counted by the limiter) deletes one copy (204). The id must match `pre-restore-YYYYmmdd-HHMMSS[-n]` and name a direct child of `data/` (404 otherwise).
- At startup, leftover `.restore-*` / `.backup-*` scratch folders in `data/` (from a crash mid-operation) are removed. Previous vaults are never removed automatically.

---

# Release 2.1: Envelope budgets (replaces the "spending money" pool)

The user chose YNAB-style envelope budgeting: you assign only money you actually have in your budget accounts, leftovers roll over, and you can plan up to 12 months ahead. This section **supersedes** the Budgets parts of "Budgets and spending" in Release 2. The review endpoint (`/api/spending/review`) is unchanged, apart from `history[].assigned` now meaning the sum of assignments. Types: `EnvelopeLine`, `BudgetAccount`, `BudgetMonth`, `BudgetSave` in `frontend/src/api.ts`.

## Data (migration v3)
- `ALTER TABLE budgets ADD removed BOOLEAN NOT NULL DEFAULT 0`. `budgets.limit_cents` now means **assigned** for that month and may be negative (see below). `budget_months` is no longer used; leave the table.
- `app_settings.budget_account_ids`: a JSON list. When absent, it defaults to every non-hidden account whose category is `bank` or `credit`.

## Definitions
- **Budget accounts** are the included accounts, restricted to non-hidden accounts whose category is `bank`, `credit` or `other`. Their **signed balance** is positive for assets and **negative** for `credit`/`loan` balances owed. `cash_total` is the sum of those balances.
- **Envelope spending** for (category, month) uses the Release 2 spending-transaction definition **restricted to budget accounts**: `spent = max(0, −Σ amount)`, so refunds net out.
- **Membership:** a category is in the budget in month m if its latest `budgets` row with `month ≤ m` exists and has `removed = false`.
- `assigned(c, m)` is the row for exactly month m (0 if there's no row, or if it's removed).
- `carryover(c, m) = max(0, available(c, m−1))` if c was a member in m−1, otherwise 0. Overspending does **not** carry into the category; it comes out of Ready to Assign instead, which follows from the formula below.
- `available(c, m) = carryover + assigned − spent`, for member months only.
- **Ready to assign** is global and computed as of today's month T:
  - `cash_total − Σ_c available(c, T) − Σ_{m > T} Σ_c assigned(c, m)`.
  - The first sum covers categories that are members in T; the second covers future assignments of categories that are members in m.
  - It's the same value on every month's view. It can be negative (over-assigned, or overspent from last month).
- **Month range:** from `earliest_month` to `latest_month = T + 12`. `earliest_month` is the earliest month with any budgets row or spending transaction, else T. Views of any month in that range are allowed. A future month has `pace = 0` and `spent = 0` unless it has dated transactions.

## Routes
- `GET /api/budgets?month=` returns `BudgetMonth`.
  - `categories` holds the members for that month, ordered by position then name.
  - `unbudgeted` is spending on budget accounts in non-member spending categories.
  - `fixed` is as before.
  - `addable` lists the spending-kind, non-hidden categories that aren't members.
  - `suggested` is the previous month's `assigned` per member category when month m has no rows at all and m > earliest_month; otherwise null. It's only a suggestion and nothing is saved.
  - `budget_accounts` lists every non-hidden bank, credit or other account, each with `included`.
- `PUT /api/budgets/{month}` takes a `BudgetSave` and returns `BudgetMonth`.
  - `assigned` upserts rows (removed = false) for the listed categories only. It's a partial update, and a new category becomes a member.
  - `removed` writes `removed = true` rows at this month for the listed categories. Any assignment rows in later months are deleted too; removing means "from here on".
  - The whole save is atomic.
- **Validation (422, with a plain `detail`):**
  - The month must be in range (T+12 max; ≥ 1900-01).
  - Categories must exist and be of kind `spending`.
  - Values must be finite.
  - **`carryover + assigned ≥ 0`** per category, so you can move leftover out but not below zero. Detail: "You can move at most $X out of Food and drink."
  - The same rule must still hold in **later** months after the save: lowering an earlier month can't shrink a leftover that a later month already moved out (the same dollars would be freed twice). Only violations the save creates or worsens are refused. Detail: "You moved $X out of Food and drink in October 2026, so September 2026 can't leave less than that behind. Change October 2026 first."
  - **If the save increases the total assigned, Ready to Assign after the save must be ≥ 0.** Detail: "Only $X is ready to assign. Lower another category first." Saves that don't increase the total are always allowed, even while Ready to Assign is negative, so the user can fix it.
- `PUT /api/budgets/accounts` takes `{account_ids: number[]}` and returns `BudgetMonth` for T.
  - Each id must be a non-hidden bank, credit or other account; otherwise 422.
  - An empty list is allowed, and then Ready to Assign is minus the assigned total.
- The budget alert (`budget` key) now fires when `spent ≥ value% × (carryover + assigned)` for member categories in T.
- The Dashboard summary is unchanged.

### Release 2.1 amendments (after review; these override the rules above where they differ)

1. **Past months are read-only.** `PUT /api/budgets/{month}` with a month before today's month returns 422 "Past months are read-only. Make changes in the current month.". Editable months run from today's month to `latest_month`. `GET` for past months still works. Overspending from an earlier month is covered in the current month.
2. **A re-added category starts fresh.** Migration v3 (still unreleased, so it's amended in place) also adds `budgets.restart BOOLEAN NOT NULL DEFAULT 0`.
   - When a save assigns a category whose row for that month is `removed = true`, it re-adds it in the month it was removed: set `removed = false` **and `restart = true`**.
   - `carryover(c, m) = 0` whenever row(c, m) has `restart = true`, so the released leftover can't come back.
   - Re-adding in a later month needs no marker, because the category wasn't a member in the month before.
3. **Over-assign rule (replaces "if the save increases the total assigned…"):** a save is refused when it **lowers** Ready to Assign **and** leaves it below 0. The detail is "Only $X is ready to assign. Lower another category first.", where X = max(0, Ready to Assign before the save). Saves that raise Ready to Assign, or leave it unchanged, are always allowed.
4. **Budget accounts are stored as exceptions**, so newly linked banks and cards join automatically. `app_settings.budget_account_ids` becomes a JSON object `{"exclude": [ids], "include": [ids]}`.
   - An account is included when it's eligible and either (its category is `bank` or `credit` and its id isn't in `exclude`) or its id is in `include`.
   - `PUT /api/budgets/accounts` computes both lists from the requested `account_ids`: `exclude` gets eligible bank or credit accounts not requested, and `include` gets requested accounts of other categories.
   - A legacy plain JSON list, from development builds only, is read as the exact set of included ids.
   - `prune_budget_accounts` removes deleted ids from both lists.

---

# Release 3: groups, targets, splits, tags, reports, debt planning, automation

This section is the authoritative contract. The TypeScript shapes are in `frontend/src/api.ts` under the section "Release 3". The Release 1, 2 and 2.1 rules still apply unless something here changes them. Money is in dollars at the API and integer cents in the DB. Months are `YYYY-MM`. "Today" means `utils.today()`. Every route requires a session.

**Spending definition update (applies everywhere):** when a transaction has **splits**, its split lines replace it in all totals. That covers budgets and envelopes, the review, reports, tags, alerts and CSV export. Each split line carries its own category and amount; the parent's own category is ignored for totals. Rules never change a transaction that has splits.

## Migration v4 (one transaction, like v2/v3)
- `category_groups`: `id` INTEGER PK, `name` TEXT NOT NULL (unique, case-insensitive), `position` INT, `created_at`.
- `categories` add:
  - `group_id` INTEGER NULL (FK category_groups ON DELETE SET NULL)
  - `target_kind` TEXT NULL (`monthly|by_date`)
  - `target_cents` INT NULL
  - `target_date` DATE NULL
- `accounts` add `loan_group` TEXT NULL. This is a custom group label for liabilities; NULL means use the automatic group.
- `transaction_splits`: `id`, `transaction_id` (FK transactions ON DELETE CASCADE, indexed), `amount_cents` INT, `category` TEXT NOT NULL (FK categories), `notes` TEXT NULL, `position` INT.
- `tags`: `id`, `name` (unique, case-insensitive), `hue` INT.
- `transaction_tags`: (`transaction_id` FK CASCADE, `tag_id` FK CASCADE), PK on both.
- `balance_snapshots` add `estimated` BOOLEAN NOT NULL DEFAULT 0.
- Seed `alert_settings` row `income` (enabled, no value).
- `app_settings` keys (absent means default):
  - `auto_backup_dir` (none)
  - `auto_backup_keep` (10)
  - `auto_sync_hours` (6)
  - `auto_backup_last_at`
  - `auto_backup_last_error`
- Test: upgrade a real v3 vault with rows.

## 1. Category groups
- `GET /api/category-groups` returns `CategoryGroup[]` = `{id, name, position, category_ids}`, ordered by position.
- `POST` with `{name}` returns 201. `PATCH /{id}` with `{name}`. `DELETE /{id}` returns 204; its categories become ungrouped.
- `POST /api/category-groups/reorder` with `{ids}`, which must be a permutation of all group ids.
- `POST /api/category-groups/starter` creates any of these groups that are missing, and assigns only **currently ungrouped** categories. It returns `CategoryGroup[]`.

  | Group | Categories |
  |---|---|
  | Home | RENT_AND_UTILITIES, HOME_IMPROVEMENT |
  | Car | TRANSPORTATION |
  | Food | FOOD_AND_DRINK |
  | Personal | PERSONAL_CARE, ENTERTAINMENT, GENERAL_MERCHANDISE, TRAVEL |
  | Health | MEDICAL |
  | Bills & services | GENERAL_SERVICES, BANK_FEES, GOVERNMENT_AND_NON_PROFIT |

- `PATCH /api/categories/{id}` also accepts `group_id: number|null`. Unknown group → 422.
- `TxnCategory` gains `group_id`. `BudgetMonth` gains `groups: {id,name,position}[]`, and `EnvelopeLine` gains `group_id`.

## 2. Budget targets
- `PATCH /api/categories/{id}` also accepts `target`:
  - `{kind: 'monthly', amount}` means assign `amount` every month;
  - `{kind: 'by_date', amount, date}` means have `amount` available by `date`, which must be today or later;
  - `null` clears it.
  - `amount` must be > 0 and finite. Only spending-kind categories can have targets.
- `TxnCategory` gains `target: CategoryTarget | null`.
- `EnvelopeLine` gains `target: {kind, amount, date, needed} | null`, where `needed` is what to assign **this month** to stay on track:
  - **monthly:** `needed = max(0, amount − assigned)`.
  - **by_date:** `months_left` = months from the viewed month to the target month, inclusive and at least 1. Then:
    - `remaining = max(0, amount − carryover)`
    - `per_month = ceil_to_cent(remaining / months_left)`
    - `needed = max(0, per_month − assigned)`
    - A viewed month after the target month gives `needed = 0`.
- `BudgetMonth` gains `needed_total` (Σ needed over members).
- `POST /api/budgets/{month}/fund-targets` (current or future month only) funds each member category with `needed > 0`.
  - Go in group position order, then category position, then name.
  - Add `needed` to its assigned amount, capped so Ready to Assign never goes below 0. The last one may be partially funded; after that, stop.
  - It's atomic and follows the Release 2.1 rules.
  - Returns `{month: BudgetMonth, funded: number, unfunded: number}` (dollars).

## 3. Split transactions
- `PUT /api/transactions/{id}/splits` with `{splits: [{amount, category, notes?}]}` sets the splits. An empty list removes them.
  - Otherwise there must be 2–20 parts.
  - Each part must be non-zero and have the **same sign** as the parent.
  - **The parts must sum to the parent amount to the cent.**
  - Categories must exist.
  - Any violation is a 422 with a plain detail.
  - Returns `Transaction`.
- `Transaction` gains `splits: TransactionSplit[]` = `{id, amount, category, category_name, category_hue, notes}` (empty when there are none).
- **Sync:** when Plaid changes a split transaction's amount, scale the parts proportionally to the new amount. Round to cents, put the remainder on the largest part, and keep signs. Removing the transaction removes its splits (cascade).
- **CSV:** one row per split part (same date, account and name). Notes read `split k/n` plus any part notes.

## 4. Change all transactions from a merchant (bulk recategorize)
Match rule: `field` `merchant` matches `merchant_name`, and `name` matches `name`. Matching is case-insensitive and exact after trimming, the same as the rule op `is`. Transactions with splits are skipped.
- `POST /api/transactions/recategorize/preview` with `{field, text, category}` returns `{matches, already, manual}`:
  - `matches`: transactions that would change;
  - `already`: those already in that category;
  - `manual`: those among `matches` that are hand-set (`category_source` `user` or `user_bulk`).
- `POST /api/transactions/recategorize` with `{field, text, category, include_manual}` returns `{changed}`.
  - It sets `category` and `category_source='user'`, so later rule runs don't undo it.
  - It skips hand-set (`user`/`user_bulk`) rows unless `include_manual` is set.
  - This is one-time and **creates no rule**. The frontend's third choice, "and future ones too", also calls `api.rules.create` as today.

## 5. Tags
- `GET /api/tags` returns `Tag[]` = `{id, name, hue, count, spent}`, where `spent` is spending over all time, split-aware.
- `POST` with `{name, hue?}` returns 201; 409 for a duplicate name. `PATCH /{id}` accepts `{name?, hue?}`. `DELETE /{id}` returns 204.
- `PUT /api/transactions/{id}/tags` with `{tag_ids}` (at most 20) returns `Transaction`.
- `Transaction` gains `tags: {id,name,hue}[]`.
- `GET /api/transactions` and the CSV export gain a `tag=<id>` filter, and the CSV gains a `tags` column (names separated by `; `).

## 6. Reports
Both reports use the spending definitions from Release 2, over all non-hidden bank, credit, hsa and other accounts. They are split-aware.
- `GET /api/reports/monthly?months=12` (1–60) returns `MonthlyReport`: `{months: [{month, income, spending, fixed, net, savings_rate, by_category: {id: spent}, by_group: {"<group id>"|"none": spent}}], categories: [{id,name,hue,group_id}], groups: [{id,name}]}`.
  - The list is oldest first and ends with the current month.
  - `net = income − spending − fixed`.
  - `savings_rate = net / income × 100`, rounded to 1 decimal, or null when there's no income.
- (Removed in Release 3.10: Year in review was dropped.) `GET /api/reports/year?year=YYYY` returned `YearReport`: `{year, income, spending, fixed, net, savings_rate, by_month: [{month, income, spending}], top_categories: [{category,name,hue,spent}] (top 10), top_merchants: [{merchant, spent, count}] (top 10), biggest_purchase: {date, name, amount, category_name}|null, net_worth_start: number|null, net_worth_end: number|null, tags: [{id,name,hue,spent}]}`.
  - Net worth comes from the snapshot history: the start is Jan 1 or the earliest date, and the end is Dec 31 or today.

## 7. Balance history backfill
- After every sync and every import, for each non-hidden `bank` or `credit` account with posted transactions, **estimate** end-of-day balances **backwards** from the current balance. At most 730 days, and never before the account's earliest transaction.
  - For `bank`: `balance(d−1) = balance(d) − Σ amounts dated d`.
  - For `credit` (balance owed, positive): `owed(d−1) = owed(d) + Σ amounts dated d`. Our amounts are negative for money out, so a purchase raises what's owed.
  - Pending transactions are ignored.
- Write snapshots with `estimated = true` **only for dates before that account's earliest real (non-estimated) snapshot**. Never overwrite a real snapshot.
- A later real snapshot for the same date replaces an estimated one, via the existing upsert, which sets `estimated=false`.
- Net-worth and account history include estimated points, and `BalancePoint` / `NetWorthPoint` gain `estimated: boolean`. A net-worth point is estimated if any of its components is.

## 8. Loans: groups, payoff graph, debt planner
**Loan groups**
- `Account` gains `loan_group: string | null`. For liabilities this is the effective group (the custom label, else the automatic one); for assets it's null.
  - Automatic groups: credit card → "Credit cards"; subtype auto → "Car loans"; student → "Student loans"; mortgage and home equity → "Home loans"; other loans → "Other loans". Manual liabilities use "Credit cards" for the credit category, else "Other loans".
- `PATCH /api/accounts/{id}` accepts `loan_group: string|null` (liabilities only, ≤ 40 chars; null resets to automatic). Otherwise 422.

**Payoff math.** Credit cards assume **no new charges**. Loop month by month:
- `interest = round_cents(balance × apr/100/12)`
- `payment = min(balance + interest, minimum + extra)`
- `balance = balance + interest − payment`

Stop at 0 or after 600 months (then `never = true`). If `minimum + extra ≤ first month's interest`, `never = true`.

**`GET /api/loans/{account_id}/payoff?extra=0`** returns `PayoffProjection`: `{account_id, name, balance, apr, minimum, extra, months, payoff_date, total_interest, never, schedule: [{month, balance, interest, principal}], baseline: {months, payoff_date, total_interest, never}, history: BalancePoint[] (last 730 days)}`.
- `schedule` starts with next month. `baseline` is the same projection with extra = 0.
- 422 when the account isn't a liability, or has no minimum payment and no `?minimum=` override. The optional query parameter `minimum` overrides a missing minimum.

**`POST /api/debt/plan`** with `{extra, strategy: 'avalanche'|'snowball'|'custom', order?: number[], account_ids?: number[]}` returns `DebtPlan`.
- Debts: the given accounts, or all non-hidden liabilities with a positive balance and a known minimum.
- Every month each debt pays its minimum. The extra, **plus the minimums freed by debts already paid off**, goes to the target debt:
  - avalanche: highest APR first; ties go to the smaller balance;
  - snowball: smallest balance first;
  - custom: the order given in `order`.
- Result: `{strategy, extra, months, payoff_date, total_interest, never, debts: [{account_id, name, loan_group, balance, apr, minimum, payoff_months, payoff_date, interest}], timeline: [{month, total, by_account: {"<id>": balance}}], compare: {avalanche: {months, total_interest}, snowball: {months, total_interest}, minimums_only: {months, total_interest}}}`.

## 9. Automatic encrypted backups
- `GET /api/backup/auto` returns `AutoBackup` = `{dir: string|null, keep: number, last_at: string|null, last_error: string|null, files: [{name, size_bytes, created_at}]}`, newest first.
- `PUT /api/backup/auto` with `{dir: string|null, keep}` (keep 1–100). `dir` rules:
  - It must be an **absolute local path** to an existing, writable directory. Test this by creating and deleting a temp file.
  - **UNC or network paths are rejected**: anything starting with `\\` or `//`, or a mapped drive whose `GetDriveType` is DRIVE_REMOTE. Otherwise Windows could send the user's credentials to another machine (the NTLM issue from the Release 1 review).
  - It must not be inside `data/`. `null` disables automatic backups.
  - 422 with a plain detail otherwise.
- **When backups are made** (only while unlocked, using the same file format as the manual backup and the same encrypted `VACUUM INTO` copy):
  - (a) when the vault locks for any reason (manual lock, auto-lock, or server shutdown in the lifespan), **before** the key is wiped;
  - (b) every 24h while unlocked;
  - either way, skipped when the last auto-backup was under 10 minutes ago.
- File name: `fintrack-auto-YYYYmmdd-HHMMSS.ftbackup`. Keep the newest `keep` files matching exactly that pattern and delete older ones. Never touch other files.
- Failures never block locking. Store `last_error` and log only the exception type.
- Each backup opens with the master password in effect when it was made.

## 10. Automatic sync and "new money to assign"
- `GET/PUT /api/plaid/auto-sync` with `{hours}`, where `hours ∈ {0,3,6,12,24}` (0 = off). Returns `{hours, next_at: string|null}`.
- A lifespan loop checks every 5 minutes: if the vault is unlocked, Plaid is configured, there are items that are neither pending nor `login_required` (Plaid can't return data for those until the user re-authenticates), `hours > 0`, and the oldest `last_synced_at` is at least `hours` old (or null), it runs the same sync as `POST /api/plaid/sync`, including after-sync processing. Errors are logged by type only.
- **The `income` alert (accent):** when a sync adds posted income-kind transactions with amount > 0 on **budget accounts**, emit one event per sync.
  - Title: "$X new to assign". Body: "{merchant or name} on {account}. Ready to assign is now $Y."
  - With several transactions, the title uses the sum and the body says "N deposits".
  - `dedupe_key` = `income:` + sorted transaction ids.
  - `AlertKey` gains `income`, and the settings order becomes low, big, budget, due, newrec, price, conn, income.

## Frontend notes
- A new **Reports** page at `/reports`, placed in the nav after Goals, with the `chart` icon.
- The Spending "Add a category…" menu gets a final "+ New category…" option that creates a spending category, then adds it to the budget.
- The Transactions bulk-change prompt offers three choices: just this one / all N {merchant} transactions (one-time) / all + future (also creates the rule). When `manual > 0` it offers a checkbox to include hand-set ones.

# Release 3.1: Budget screen, "Assign every dollar" (frontend only)

The Spending page's budget table is replaced by the budget-assign design (its notes aren't in the repo, because their sample data is the real budget). No API or data changes: it uses `GET/PUT /api/budgets/{month}`, `POST /api/budgets/{month}/fund-targets` and `PUT /api/budgets/accounts` as specified above. The design README's own backend and Ready-to-assign sections describe a prototype. The Release 2.1 rules above still apply (budget accounts, carryover, the over-assign rule, past months read-only).

- **Saving as you go:** Assigned cells are always editable. Enter or blur saves one category (a partial `assigned` update), and Esc discards the typing. Saves run one at a time. Values below −carryover are raised to it. Raising a category stops where Ready to assign reaches $0, with a toast; the server refuses anything further.
- **Move money** saves both categories in one PUT, so Ready to assign doesn't change. It can move at most `carryover + assigned` out of a category. Moving more than Available is allowed, and the category turns overspent.
- **Assign menu:**
  - "Fund underfunded" calls `fund-targets`.
  - "Assign what you did last month" sets every member to the previous month's `assigned`, or to 0 if it had none.
  - "Budget accounts" opens the accounts picker.
- **Quick assign:** underfunded (assigned + target `needed`), assigned last month, spent this month, and reset to zero.
- **Undo** restores the month's previous assigned amounts. It keeps up to 40 snapshots per page visit and is cleared on changing month.
- **Status** is checked in this order: overspent (available < 0), underfunded (target `needed` > 0), money available (> 0), zero. The filter pills use the same order.
- **Month range:** planning months (up to `latest_month`) stay reachable. Past months are view only: no inputs, Move or Quick assign.
- Unbudgeted spending, "+ New category…", the target modal, rename, remove from budget and the monthly review stay, and now sit in or below the new layout.

# Release 3.2: Reports overview

The Reports page's Monthly tab becomes an **Overview** built around three questions: where the money goes (a drill-down from groups to categories to one category), what's changing (this month's pace against the usual), and which habits could improve (a spending calendar, weekday pattern, small purchases and suggested challenges). Cash flow moves to the bottom. Year in review is unchanged. The design handoff isn't in the repo, because its sample data is the real budget.

- **Bills:** a category is a fixed bill when it's `fixed` kind, or when at least 75% of what it spent over the last 6 months were payments of a Recurring item that isn't dismissed. A payment must match the item's merchant, its account (an item without an account matches any) and its amount within 20%. Habits judge bills as of the month viewed. "Hide fixed bills" leaves bills out of the stats and the breakdown. Habits always leave them out.
- **Pace:** the current month's amount ÷ (day of month ÷ days in month). It's calculated on the client and only for categories that aren't bills. "Usual" is the average of up to 5 full months before the current one, counting from the first month with any activity.
- `GET /api/reports/monthly` also returns `months[].by_fixed: {id: spent}` (fixed-kind categories). `categories` now lists the fixed categories after the spending ones, and each category has `kind` and `bill`.
- `GET /api/reports/category/{id}?start&end&months=6` (months 1–24; the range is at most 5 years, from 1900 on). It returns `{category, months: [{month, spent}], merchants: [{name, spent, count}] (top 5), biggest: [{id, date, name, amount}] (top 5 transactions; a split's parts in the category add up)}`. Months end with `end`'s month; merchants and biggest cover start through end. It returns 404 for an unknown category or one that isn't spending or fixed.
- `GET /api/reports/habits?month=YYYY-MM&under=15` (under is 0.01 to 1000). It returns `{month, through, days: [{date, spent}], small: {under, count, total, merchants: [{name, count, total}] (top 5 by total)}}`. It covers day-to-day spending only: spending categories that aren't bills. Days run from the 1st through today; `through` is null for a future month. A day's `spent` never goes below 0. A small purchase is a transaction whose day-to-day outflow is greater than 0 and less than `under`.
- **Challenges** ("Try this next month") are generated from the data. Starting one only saves a UI preference for next month. Creating a budget target from a challenge isn't designed yet.

# Release 3.3: Transactions redesign

The Transactions page becomes the "Day groups" layout from the transactions design (its notes aren't in the repo, because their sample data is the real budget): infinite scroll under sticky day headers, filter pills (All · Needs a category · Money in · Money out), suggested categories, a details side panel, several transactions at once, and Undo.

**No migration, no schema version change.** `category_source` is a TEXT column with no CHECK, so it simply gains the value `user_bulk` ("Set by you (several at once)"), treated exactly like `user` everywhere a category counts as hand-set: rule runs, sync, and bulk recategorize (`manual`, `include_manual`). *Old-build caveat:* a build from before Release 3.3 opening the same vault treats `user_bulk` rows as automatic, so its next rule run or sync may re-categorize them.

## Needs a category
"Anything not in the budget." A transaction needs a category when all of these hold:
- it has no splits, `is_transfer` is false, and `category_source = 'plaid'` (hand-set and rule-set rows never need one, even when ungrouped);
- and its category is NULL, `OTHER`, an id that no longer exists, a hidden category, **or**, when at least one category group exists, a category with no group whose kind isn't `transfer`. With no groups at all, that last case is off.

Pending rows count too. The SQL clause and the per-row check live side by side in `services/txns.py` and are tested to agree.

## Shared filters
`GET /api/transactions`, `/summary`, `/ids` and `/export.csv` take the same filters: `account_id` (int), `search` (≤ 200), `start`, `end` (dates), `category` (≤ 64), `tag` (1..2^63−1) and `view` = `all | needs_category | in | out` (default `all`; anything else is 422). `in` is amount > 0, `out` is amount < 0; a $0 transaction is in neither.

**Money in/out totals** (summary and day totals) leave out `is_transfer` rows, like the spending definitions; the lists still show them. `money_out` is signed (≤ 0).

## `GET /api/transactions` (additive)
- New `cursor` (≤ 40 chars, must match `^\d{4}-\d{2}-\d{2}\.\d{1,19}$` and be a real date and an id ≤ 2^63−1, else 422): returns the rows strictly after it in `date DESC, id DESC` order. `cursor` together with `offset > 0` is 422. `offset` still works for old callers. `limit` is 1..500 (clamped).
- Response: `{items, total, next_cursor, days}`. `total` counts every row for the filters. `next_cursor` is `"YYYY-MM-DD.<id>"` of the last item, or null at the end. `days` has one entry per date on this page, `{money_in, money_out}` over the **whole day** for the full filter set (not just this page), transfers left out.
- Every `Transaction` (list, PATCH, PUT splits/tags, bulk, restore) gains:
  - `needs_category: bool`;
  - `suggested_categories: string[]`: up to 2 category ids, only for rows that need a category. They're the categories most used on past transactions from the same merchant (key: `merchant_key(merchant_name, name)`, so rows with no merchant match on the cleaned name). Split and transfer history is ignored. `OTHER`, hidden categories and any category that would itself leave the row needing a category are never suggested. Ranked by count, then most recent use, then name. Computed per page in a fixed number of queries.
  - `matching_rule_id: int | null`: the first enabled rule (any action) whose match is true for the row; null for split rows. Unlike `rule_id`, it's shown for hand-set rows too.
- `category_source` is `plaid | rule | user | user_bulk`.

## `GET /api/transactions/summary`
`{total, first_date, money_in, money_out, needs_category, counts: {all, needs_category, in, out}}`. Everything but `counts` uses the filters **and** `view` (`first_date` = the earliest date, null when nothing matches; `needs_category` = the matching rows that need a category). `counts` use the filters without `view`.

## `GET /api/transactions/ids`
The shared filters plus `needs_category: bool = false`. Returns `{ids, total, truncated}`: ids in list order, at most 1000.

## `PATCH /api/transactions` (several at once)
Body `{ids: [int], category: string | null}` (both required; unknown fields are 422). `ids`: 1–1000 ids ≥ 1, duplicates ignored. `category`: 1–64 chars and must exist (422 "unknown category"), or `null` = reset to automatic.
- Any id that doesn't exist: 404 "transaction not found" and **nothing changes**. Everything is validated before anything is written, in one DB transaction.
- Split transactions are skipped and listed in `skipped`.
- With a category: rows not already hand-set to it get the category, `rule_id = null`, and `category_source = 'user_bulk'` when 2 or more rows change, else `user`.
- With `null`: rows go back to `plaid` (`category = plaid_category`, `rule_id = null`), then rules are re-applied once.
- Response `{items: Transaction[], previous: [{id, category, category_source}], skipped: [id]}`. `items` and `previous` list only the rows that changed, in request order; `previous` is their state before, for Undo.

## `POST /api/transactions/restore` (Undo)
Body `{items: [{id, category: string | null, category_source: 'plaid' | 'rule' | 'user' | 'user_bulk'}]}`, 1–1000 items, each id once (422 otherwise). An id that doesn't exist: 404, nothing changes.
- `user` / `user_bulk`: `category` is required and must exist (else 422); it's set with that source and `rule_id = null`.
- `plaid` / `rule`: back to automatic, `category` is ignored. The rules decide the category and `rule_id` again: a client can never set `rule_id` or claim a rule.
- Split transactions are skipped. Rules are re-applied once if any row went automatic.
- Returns `{items: Transaction[]}` (the restored rows).

Undo on the page: delete the rule the toast created, if any (a 404 is fine), then restore the snapshot, then reload.

## `GET /api/transactions/{id}/related`
`{field, text, count, total, first_date}` for the details panel footer. `field` is `merchant` when the transaction has a merchant name, else `name`; `text` is that value trimmed. It counts every transaction (splits and transfers included) whose same field matches exactly after trimming and ignoring case, like the bulk recategorize. `total` is signed. 404 for an unknown id.

## Unchanged, reused
`PATCH /api/transactions/{id}` (a single set is `user`), `PUT …/splits`, `PUT …/tags`, `POST /api/transactions/recategorize[/preview]`, `/api/rules*`, `/api/categories`, `/api/category-groups`. "Always for {merchant}" is `POST /api/rules` (`field` merchant, or name when there's no merchant; `op` is; trimmed text ≤ 200), which re-applies rules to that merchant's automatic rows; it doesn't call `recategorize`.

## Security
All new routes need a session and the CSRF header. Bodies reject unknown fields and NaN. Queries use bound parameters only (the cursor is parsed into a date and an int), and long id lists are queried in chunks of 900. Merchant names, notes and bodies aren't logged.

# Release 3.4: Bank connection keys

Plaid keys can be entered in the app (Settings → **Bank connection**) instead of `.env`. They're stored inside the encrypted vault, so a non-technical user never edits a file or restarts the server.

## Storage (no migration, schema version unchanged)
Three `app_settings` rows, written and deleted together:
- `plaid_keys_meta`: JSON `{"v":1, "client_id", "env":"sandbox"|"production", "secret_hint":"<last 4>", "saved_at":"<ISO Z>"}`. Not secret; status reads only this row, so a GET never loads the secret.
- `plaid_keys_secret`: the secret (encrypted at rest by SQLCipher, like everything else in the DB).
- `plaid_keys_last_test`: JSON `{ok, code, message, plaid_code, at, env}` of the last check of the saved keys (written by a successful save and by `POST /test` with `{}` on the vault's keys).

Malformed rows count as "no keys" (a warning with the exception type only is logged). An older build ignores the rows.

## Precedence (exact)
- `source = "env"` iff both `PLAID_CLIENT_ID` and `PLAID_SECRET` are non-empty (process environment or `.env`). Then the `.env` client is used, vault keys are neither loaded nor used, `PUT` is 409 `managed_by_env`, and removing stored vault keys is still allowed. `secret_hint` is omitted (null) for an `.env` secret shorter than 16 characters.
- Otherwise `"vault"` if the vault has stored keys, else `"none"`. Exactly one of the two `.env` values set → `env_file_partial: true` and the vault/none path is used.
- At runtime: `AppState.active_plaid()` = the `.env`/injected client if configured, else the vault's client while unlocked, else the unconfigured client. Routes (`get_plaid_client`) and auto-sync (`sync_tick`) both use it.

## Lifecycle
- The vault's client is built after every unlock, setup, restore and password change (`Vault.after_unlock`) and dropped after every lock: manual, idle, shutdown and restore's pre-install lock (`Vault.after_lock`, run from `_lock_now`). A failing hook is logged by type and never blocks unlocking or locking.
- Clients are immutable; changing keys builds a new one and swaps the reference, so an in-flight request or sync finishes with the client it captured.
- Restoring a backup reactivates the keys saved in it (possibly an old or rotated secret).

## Checking keys
- `/institutions/get` (`count=1, offset=0, country_codes=["US"]`): read-only, free, and independent of product approval. Timeout `(5, 20)` s. Always through a throwaway client from `state.plaid_factory`; nothing changes until a save commits. Product access is only proven at link time.
- **Linked Items** (any `plaid_items` row, pending included): the keys must own them. First a local check: every access token's env (`access-<env>-…`) must equal the keys' env, else `items_mismatch` with no network call. Then `/item/get` on the first Item (by id): success, or an `ITEM_ERROR` that proves Plaid found the Item under these keys (`ITEM_LOGIN_REQUIRED`, `PENDING_EXPIRATION`, `PENDING_DISCONNECT`, `ITEM_LOCKED`, `INVALID_UPDATED_USERNAME`, `INVALID_CREDENTIALS`, `INSUFFICIENT_CREDENTIALS`, `USER_SETUP_REQUIRED`, `MFA_NOT_SUPPORTED`, `NO_ACCOUNTS`, `ITEM_NOT_SUPPORTED`, `ACCESS_NOT_GRANTED`) = owned; any other `ITEM_ERROR` (e.g. `ITEM_NOT_FOUND`) or `INVALID_INPUT` = not owned → `items_mismatch`; anything else → `unreachable`/`plaid_error`. The probe runs on `PUT` and on `POST /test` with `{}` (the active keys) only: candidate keys on `/test` get just the local check, so access tokens are never sent with keys not yet confirmed by the password. So a rotated secret passes, while switching to another Plaid account or env is blocked until the Items are removed (while the old keys can still `/item/remove` them).
- **`env: "auto"`** (`POST /test` with candidate keys only): with linked Items, only the env their tokens name is tried (tokens naming several envs → `items_mismatch`); otherwise production, then sandbox, first to pass wins. A network failure stops at once. If no env works, the reported code is the first of `unreachable`, `plaid_error`, `items_mismatch`, `wrong_environment`, `invalid_keys` that occurred, and `env` is null. One auto check counts as one test for the throttle.

Result codes (the server owns the messages; `plaid_code` is Plaid's `error_code` matching `^[A-Z0-9_]{1,64}$`, else `"UNKNOWN"`; Plaid's `error_message` is never returned):

| code | when | HTTP on PUT |
|---|---|---|
| `ok` | the key check passed (and the Item probe, if any) | 200 |
| `invalid_keys` | `INVALID_API_KEYS`, or any other `INVALID_INPUT` on `/institutions/get` | 422 |
| `wrong_environment` | `UNAUTHORIZED_ENVIRONMENT` | 422 |
| `items_mismatch` | a token's env differs, or the probe says not owned | 409 |
| `unreachable` | network error or timeout | 502 |
| `plaid_error` | anything else (rate limit, Plaid outage, …) | 502 |

## Routes (session + CSRF; unknown fields 422; validation errors never echo input)
Keys: `client_id` and `secret` are trimmed and must match `^[A-Za-z0-9]{16,64}$`; `env` is `sandbox|production`; `current_password` is ≤ 4096 chars (empty → the friendly 422 below).

`PlaidKeysTest` = `{ok, code, message, plaid_code: str|null, at, env: "sandbox"|"production"|null, checked_linked_items: bool}`. `env` is the env that worked, or the env tried for a single-env check; null when an auto check found none. `last_test` in the status has the same shape without `checked_linked_items`.

- `GET /api/plaid/keys` → `{source, client_id, env, secret_hint, saved_at, last_test, vault_keys_ignored, env_file_partial, linked_items}`. For `.env` keys: their client ID, env and hint; `saved_at` and `last_test` null. `vault_keys_ignored` = source is env and vault rows exist.
- `POST /api/plaid/keys/test` with `{client_id, secret, env}` (env may be `"auto"`) checks candidate keys and stores nothing; with `{}` checks the active keys and, for vault keys, records `last_test`. 200 `PlaidKeysTest`. 409 `{"detail":"No Plaid keys are set yet.","code":"not_set"}` for `{}` with none; 422 for a partial body; 429 `{"detail":"Wait a moment before testing again.","retry_after":n}` + `Retry-After` (one check at a time, ≥ 2 s between starts; separate from the password limiter).
- `PUT /api/plaid/keys` `{client_id, secret, env, current_password}`: 409 `managed_by_env` (checked before the password, so nothing is counted); then the password (422 "Enter your current Iron Owl password to change your Plaid keys.", 401 "Your current Iron Owl password is incorrect.", 429 shared limiter); then the check with no DB transaction open; if the linked Items' tokens changed during the check, 409 `{"detail":"Your linked banks changed while Iron Owl was checking these keys. Please try again.","code":"items_changed"}` and nothing is stored; on success the three rows are written in one transaction, the client is reloaded and 200 returns the status. A failed check returns `{"detail": <message>, "code": <code>, "plaid_code"?: str}` with the status from the table above; nothing is stored and the active client is unchanged.
- `POST /api/plaid/keys/delete` `{current_password}` → 204, idempotent, allowed for any source (422/401/429 as for PUT). Linked Items stay but stop syncing; deleting one then only removes it locally.

## Security
- The secret is write-only: request models use `SecretStr`, responses carry only the last 4 characters, `PlaidClient.__repr__` has no keys, `Configuration.debug` is never set, and logs name only the env (`plaid keys saved (env=…)`, `plaid keys removed`) or exception types.
- The secret is held in memory only by the vault's client, only while unlocked and not `.env`-managed. Dropping it on lock is best effort (a Python `str` can't be wiped).
- Saving and removing need the master password (shared limiter): a session left unlocked must not be enough to send bank data to someone else's Plaid account.
- Backups (manual, automatic, pre-restore copies) contain the keys, encrypted with the password current when each was made. Giving someone a backup and its password gives them the Plaid secret as well as the access tokens.

# Release 3.5 (FinTrack 1.4.0): Home "Today"

The Dashboard becomes **Home**: does anything need me, how much is in checking and savings, which bills are coming, am I overspending; net worth at the bottom. Design: the home-today design notes (not in the repo).

## Version
`backend/app/version.py` holds `__version__ = "1.4.0"`. It is `FastAPI(version=…)` and was `GET /api/home`'s `version` (superseded by Home v2, Release 3.9: `GET /api/home` is removed and `GET /api/dashboard` carries no `version`; Settings shows it); it is never in an unauthenticated response (`/api/auth/status` has none; `/openapi.json` exists only with `debug`).

## Schema v5 (migration)
`ALTER TABLE alert_events ADD COLUMN data TEXT` (nullable JSON). The `price` alert writes `{"recurring_id", "old_cents", "new_cents", "cadence"}` (signed cents, as stored). Older events keep `NULL`; "Clear all" sets `data` to `NULL` along with the text. Backups made from now on have `schema_version` 5, which older builds refuse.

Same transaction: `ALTER TABLE plaid_items ADD COLUMN last_error_at DATETIME` (nullable). Every failed sync attempt of an item sets it to now (UTC); it is never cleared (the item's `status` says whether it is failing now).

## Pre-migration safety copy (every schema upgrade, from this release on)
Before `Database.open` upgrades an existing vault (`user_version` < LATEST; never for a fresh vault, and never for a backup being restored, which is upgraded in its staging folder), the vault first copies its files, byte for byte and still encrypted, to `data/pre-migrate-v<old version>-<YYYYmmddTHHMMSSZ, UTC>[-n]/`: `fintrack.db`, `keyfile.json`, and `fintrack.db-wal`/`-shm`/`-journal` and `keyfile.json.new` when present.
- **When**: after the key is checked and the version read, before anything is written (not even the v0→v1 stamp). FinTrack never switches SQLCipher to WAL, and the key check's first read has already rolled back any hot journal, so at that point the pool's only connection is idle with no transaction open and nothing is writing the file; sidecar files that exist anyway are copied alongside, so the copy is complete either way.
- **How**: the folder is created and restricted to the current user (`restrict_to_owner`, the data folder's ACL helper) before any file is copied into it; each file is written with exclusive create, flushed and fsynced (and the folders fsynced where the OS allows it).
- **Fails closed**: if any step fails, the partial folder is removed, nothing is migrated, the vault stays locked and unlock answers 500 with `PreMigrationCopyFailed` ("Iron Owl needs to update your data, but couldn't save a safety copy first, so nothing was changed. Check that the drive has free space, then try again."). The next unlock tries again.
- **Rotation**: after a successful copy only the 3 newest `pre-migrate-*` folders (by their UTC stamp) are kept; older ones are deleted, best effort. Other folders are never touched.
- Each copy opens with the password the vault had at the time (the keyfile is next to it). Recovering one is manual: lock/stop FinTrack and move its files back into `data/`.
- Startup cleanup (`cleanup_staging`, `.backup-*`/`.restore-*` only), the previous-vaults list and delete (`pre-restore-*` only), backups (the database and keyfile only) and restore (moves only the vault's own files) all ignore these folders.

## `GET /api/home`
*(Removed in Home v2, Release 3.9: see `GET /api/dashboard`. Only the dismissal routes below remain.)*

Session + unlocked (401 `locked` otherwise). One call for the whole screen. Each section is built field by field: access tokens are never loaded, the backup folder's full path is never returned, and Plaid's free text never appears (`error_code` is Plaid's code when it matches `^[A-Z0-9_]{1,64}$`, else `"UNKNOWN"`).

`HomeData` = `{today, version, setup, banks, cash, budget, forecast, low_alert_enabled, backup, net_worth, needs, dismissed, errors}`:
- `setup` = `{plaid_configured, plaid_source: "env"|"vault"|"none", keys_rejected, visible_accounts, linked_items, pending_items}`. `plaid_*` as `GET /api/plaid/status`. `keys_rejected` = keys saved in the vault (not `.env`) whose last check failed with a code other than `unreachable`/`plaid_error`. `visible_accounts` = non-hidden accounts; `linked_items` = items that aren't pending; `pending_items` = pending items.
- `banks` = every Plaid item, by id: `{item_id, institution_name, kind, status, error_code, last_synced_at}`.
- `cash` = `{total, updated_at, accounts: [{id, name, mask, institution_name, balance, source: "plaid"|"manual", item_id, subtype}]}`: non-hidden `bank` accounts by name. `updated_at` = the newest `last_synced_at` of their items (null if all are manual).
- `budget` = `{month, is_current, days_left, has_budget, planned, spent, left}` from `GET /api/budgets` for today's month: `left` = `available`, `planned` = `available + spent`, `has_budget` = the month has envelopes.
- `forecast` = exactly `GET /api/forecast` (default end). `low_alert_enabled` = the `low` alert setting.
- `backup` = `{enabled, folder_name, last_at, last_error_at, last_error_code}`, from the settings rows only (the folder is never listed or touched). `folder_name` is the folder's own name (`"D:"` for a drive root).
- `net_worth` = `{total, assets, liabilities, by_category}`, the same totals as `/api/summary`.
- `cash`, `budget`, `forecast` and `net_worth` are built separately: one that fails is `null` and its name is added to `errors`, so the page can offer Try again on that card. Each alert kind is built separately too: a failing kind is left out and `"needs"` is added to `errors`. Only the exception type is logged.

### `needs` ("Things that need you")
`HomeAlert` = `{key, kind, tone: "act"|"warn"|"info", dismissible, fingerprint, data}`, sorted by tone (act, warn, info), then by kind in this order:

| kind | key | tone | when | fingerprint | data |
|---|---|---|---|---|---|
| `bank_signin` | `bank_signin:{item}` | act | item `login_required` | `""` (not dismissible) | `{item_id, item_kind, institution_name, error_code}` |
| `bank_error` | `bank_error:{item}` | warn | item `error` | `{error_code[:40]}:{last_error_at or last_synced_at or "never"}` (so "Not now" lasts until the next failed attempt) | same |
| `finish_setup` | `finish_setup:{item}` | info | item `pending` | item id | `{item_id, institution_name}` |
| `low_balance` | `low_balance:{account}` | warn | built by the client from the forecast | dip date | (client) |
| `backup_failed` | `backup_failed` | warn | a backup folder is set and the last automatic backup failed | `last_error_at` | `{code, folder_name, at}` |
| `needs_category` | `needs_category` | info | transactions dated from the 1st of this month that need a category (the rows `/transactions?view=needs_category&start=YYYY-MM-01` shows) | `t{newest such id}` | `{count, month_start}` |
| `price_up` | `price:{event}` | info | `price` events of the last 30 days, read or not, not cleared, for a charge that went up; the newest per recurring item; at most 10 | event id | `{recurring_id, name, amount, previous, cadence}` |

`price_up` from an event without `data` (before v5): the recurring id comes from `dedupe_key` `price:{item}:{txn}`, `amount` from that transaction (else the item), `previous` is null and `cadence` comes from the item. `recurring_id` is null (and `name` comes from the title) when the item was deleted. Deposits that went up aren't shown. Connection (`conn`) events are ignored: the item's status is the source of truth.

### "Not now"
`dismissed` = `{key: fingerprint}`, stored in the vault as `app_settings.home_dismissed` (oldest first, at most 100; the oldest are dropped). The client shows an alert when it isn't dismissible or `dismissed[key] !== fingerprint`, so it comes back when something changes. Exception, `needs_category`: it stays hidden while the current `t{id}` is at most the dismissed one (ids compared as numbers), so it comes back only when a newer purchase needs a category, not when older ones get categorized.
- `PUT /api/home/dismissals/{key}` `{fingerprint}` → 204. `key` must match `^[a-z_]{1,32}(:[A-Za-z0-9_.-]{1,40})?$` (else 422 `invalid alert key`); `fingerprint` is 1–64 of `[A-Za-z0-9:._-]`; unknown fields 422. Dismissing again moves the key to the newest.
- `DELETE /api/home/dismissals/{key}` → 204 (also when it wasn't dismissed).

## Automatic backup status and "Back up now"
- The runner also stores `auto_backup_last_error_at` (ISO Z) and `auto_backup_last_error_code`: `missing` (folder or drive gone), `denied` (permission), `write` (other write errors), `network` (the folder now leads to a network share), `other`. Both are cleared by a successful backup and when backups are turned off. `GET /api/backup/auto` gains `last_error_at` and `last_error_code`.
- `POST /api/backup/auto/run` (body `{}` or none; unknown fields 422) → 200 `{ok, backup}` (`backup` as in `HomeData`; `ok` is false when the backup failed, and the error is recorded). 409 `{"detail":"Choose a backup folder first."}` without a folder. At most one start a minute, whether the last one worked or failed: 429 `{"detail":"FinTrack just tried. Wait a minute, then try again.","retry_after":n}` + `Retry-After`. It runs `AutoBackups.run` with no gap and **without pruning** (pressing it never deletes older automatic backups; the next automatic backup trims the folder to `keep`), serialized with the lock and periodic backups; the folder is re-validated before writing and never listed. Only the exception type is logged.
- The Home status line reads "✓ Last backup: {relative}", or "! Last good backup: {relative or never}" while the last automatic backup is failing. A sync with failures toasts "One bank needs attention" (or "{n} banks need attention") with "Home shows what to do next."; the `/` page is labeled "Home" in the menu and the command palette.

## Security
All routes need a session, and the CSRF header for non-GET. Responses are built field by field; access tokens, the full backup path and Plaid's `error_message` never appear. Dismissal keys and fingerprints are validated and stored encrypted with the rest of the vault. Deep links carry dates only.


# Packaged start on Windows (B0 + launcher)

For a PC without Python or Node: an embedded CPython 3.11 x64, a stdlib-only launcher and an Edge app window. The owner's git checkout (`start.ps1`) is unchanged apart from the points marked "owner".

## Settings (config.py)
- `FINTRACK_HOME` (read from the real environment only): the settings file becomes `FINTRACK_HOME/fintrack.env` and the default `data_dir` becomes `FINTRACK_HOME/data`. Unset: the repo layout as before. An explicit `DATA_DIR` still wins.
- `FINTRACK_WEB` → `frontend_dist` (default `frontend/dist`). `FINTRACK_INSTALL_ROOT` → `fintrack_install_root` (unset = not an installed copy; the updater stays off). `repo_root` is injectable for tests.
- `SUPPORT_CONTACT` (`support_contact`, printable characters, trimmed, cut to 60 (a longer value is shortened, never refused); default empty = "the person who set up Iron Owl").
- `EXIT_WHEN_UNUSED_SECONDS` (default 0 = off, the owner's copy; the launcher passes 180). `CONTROL_SECRET` (per launch from the launcher, ≥ 32 characters when set; empty = health proof and shutdown off).

## `GET /api/health` (no session, never activity, not a heartbeat; Host allowlist applies)
`{app:"fintrack", version, boot_id, window_open, web}`: `boot_id` is 16 random hex per server start; `web` = `frontend_dist/index.html` exists; `window_open` = some window (tab id) was heard from (status or alive) within 100 s and isn't closing (the launcher then focuses it). With a control secret, a request header `X-FinTrack-Control-Challenge: <16-128 of [A-Za-z0-9_-]>` adds `proof` = hex HMAC-SHA256(secret, `"fintrack-health-v1\n" + nonce + "\n" + boot_id`); a malformed challenge is 400. The version is public here (low sensitivity; no CORS).

## `POST /api/app/shutdown` (CSRF header; `X-FinTrack-Control: <secret>`)
Body `{}`/none or `{"restart": bool}` (unknown fields 422). 404 without a configured secret, 403 on a wrong or missing one (constant-time compare, not counted). 202 `{ok:true, exit_code}`, then `app.state.request_exit(0 | 75)` 0.2 s later. The vault locks (and backs up) on the way out, as on every exit.

## Exit codes and self-exit
`app.state.request_exit(code)` stops the server; `run_packaged.py` exits with that code: 0 = stopped, 75 = restart (the launcher re-reads `state.json`). In `run.py` (owner) it only logs. The self-exit watcher ticks every 30 s: a tick with a heartbeat since the last one, or with the vault unlocked, resets the count; otherwise one miss; after `ceil(EXIT_WHEN_UNUSED_SECONDS / 30)` misses in a row → exit 0 (once). Counting ticks (not clock differences) makes it sleep-safe. The heartbeat is `POST /api/app/alive` only (a request refused by the Host/CSRF checks or without a valid tab id doesn't count); no other request keeps the server up.

## `run_packaged.py`
Needs `FINTRACK_HOME` (else exit 2). Before importing uvicorn or the app: root logging → `FINTRACK_HOME/logs/server.log` (1 MB × 3), `sys.stdout`/`sys.stderr` → that log (pythonw gives None), faulthandler → `logs/fault.log`. Then `sys.path`: the folder holding `app/` (itself in the repo, `backend/` in a package) and `site.addsitedir(INSTALL_ROOT/runtimes/<deps_id>/site-packages)` (deps_id from `release.json`, `^[0-9a-f]{16}$`). Loopback guard as `run.py`; `uvicorn.Server` with `log_config=None`, `use_colors=False`, `access_log=False`, `proxy_headers=False`, `server_header=False`. Console tools the app runs (`whoami`, `icacls`) use `CREATE_NO_WINDOW`.

## Requirements (owner)
`backend/requirements.txt` = runtime only (packaged). `backend/requirements-dev.txt` = `-r requirements.txt` + pytest, httpx. `start.ps1` installs requirements-dev.txt and stamps both hashes; it opens the browser when `/api/health` answers (≤ 60 s) instead of after a fixed 2 s.

## Launcher, install, build
See `packaging/launcher/launcher_core.py` (module docstring: layout and the `state.json` contract with the updater), `packaging/launcher/launch.pyw`, `packaging/install.ps1`, `packaging/uninstall.ps1`, `tools/release/build_package.ps1`, `tools/release/deps_id.py`. Launcher codes (log only): FT-START-01 no healthy answer in 45 s; 03 server exited while starting; 04 no free port 8000-8010; 05 files/runtime/state missing or `web:false`; 06 no browser; 07 health proof failed (another program on the port → next port); 08 the server named in `run/control.json` still runs this install's `python\pythonw.exe` but doesn't answer and couldn't be stopped (it is stopped first, never run twice). Update codes written to `data/updates/update_result.json`: FT-UPD-01 (pre-update copy failed), FT-UPD-03 (new version didn't pass the 60 s health gate), FT-UPD-05 (server-requested rollback). FT-UPD-06 (log + message box only): the pre-update copy couldn't be put back; the live files stay in place, `state.json` is unchanged (rollback still pending) and no version is started.

---

# Release 3.6: Recovery sheet (forgotten password)

A printed page of 36 digits that lets the user choose a new password. Design: the recovery design notes (not in the repo; their MAPPING.md is the agreed contract). Security wins over the design; the design wins on copy, screens and flow.

## The code
- 6 groups × 6 digits. Each group = 5 random digits (`secrets.randbelow`) + a **Damm** check digit over `str(group_number) + data` (group_number 1..6), so a typo is found per group and a group typed in the wrong box fails too. 30 random digits ≈ 99.7 bits. Shared test vectors: `shared/recovery-code-vectors.json` (backend and frontend).
- Input: only ASCII `0-9`, spaces and dashes; exactly 36 digits after removing them (else 422 `bad_format`). Groups whose check digit fails → 422 `typo` + `groups` (1-based). Neither is counted by the limiter; both are decided before any Argon2 work.
- Shown and printed as `739 151`. The **sheet number** (`482-913`) = `int.from_bytes(sha256(b"fintrack-sheet" + public_key)[:4], "big") % 1_000_000`, zero-padded. It isn't secret, and is always derived, never stored.

## Crypto (`backend/app/recovery.py`, `cryptography` package)
- seed = Argon2id(the 30 data digits as ASCII, 16-byte recovery salt, t=3, m=65536 KiB, p=4, 32 bytes); X25519 private key from the seed; only the **public key** is stored.
- Seal the vault key K: ephemeral X25519 key `e`; `shared = e.exchange(pub)`; `wk = HKDF-SHA256(shared, 32, salt=None, info="fintrack/recovery-wrap/v1" + epk + pub)`; `ct = AES-256-GCM(wk).encrypt(nonce12, K, aad="fintrack-recovery-v1|" + for_salt)`, where `for_salt` is the keyfile salt of the password key being sealed.
- Recover: derive the seed, compare public keys (constant time), open the sealed key, and accept it only if it opens the database.
- **MAC**: `HMAC-SHA256(HKDF-SHA256(K, 32, info="fintrack/recovery-mac/v1"), canonical JSON of the block without "mac")`. Whenever K is at hand, the MAC is checked before the block's public key is trusted for a re-seal (change password, recover, new sheet, restore). A block that fails it is never re-sealed (it shows as "Needs a new sheet"). This stops someone who can write `data/` but doesn't know K from planting their own sheet.
- **Public key on record**: the SHA-256 (hex) of the active sheet's public key is kept inside the encrypted database (`app_settings.recovery_active_pub`; `recovery_prev_pub` briefly during a new sheet's rekey). A block is trusted (re-sealed by change password, carried over by restore, retired by a new sheet, confirmed, or shown as active) only if its MAC checks out *and* its public key is on record; otherwise it is "stale" and never re-sealed. So an older block put back into `keyfile.json` is never revived, even one that was validly MAC'd in its time. (Recover can't read the database before it opens; there the block's own sealed key has just opened the vault, which, with key rotation, only the active block can do.)

## `keyfile.json` (still version 1)
`{"version":1,"kdf":"argon2id","salt","time_cost","memory_cost","parallelism","recovery":{"version":1,"created_at","confirmed","kdf":"argon2id","salt","time_cost","memory_cost","parallelism","public_key","sealed":{"alg":"x25519-hkdf-sha256-aes256gcm","for_salt","epk","nonce","ct"},"retired":[{"public_key","created_at","retired_at"}],"mac"}}`.
- Parsed strictly: exact keys, canonical base64 with exact lengths (salt 16, public_key 32, epk 32, nonce 12, ct 48, mac 32; for_salt 16–64), the same KDF bounds as the password keyfile, `retired` at most 10, ISO `…Z` timestamps. A malformed block counts as "no sheet" (the password still works; never a 500); errors and logs never include input.
- A new sheet made in Settings keeps the recovery KDF salt of the current (valid) block, so one Argon2 run recognizes both the current sheet and its retired ones.
- Existing vaults have no block: no migration; Settings and Home offer to make one. **Downgrade**: an older build ignores the block; its password change writes a keyfile without it, so the sheet then stops working (make a new one after upgrading again).

## Vault flows
- **Setup** creates the sheet with the vault: the block is active with `confirmed:false`; the groups are returned once in the setup response. The setup screen's Continue calls `POST /api/recovery/confirm`. Closing early leaves status `unconfirmed` (the written code still works).
- **Change password** re-seals K' to the same public key (`_rekey_to`, shared with recover), crash-safe through `keyfile.json.new` as before. The sheet keeps working after any number of password changes.
- **Recover** (`POST /api/auth/recover`, no session): guard → format/typo → password policy → a block exists → Argon2: try `keyfile.json`, then `keyfile.json.new` (a match there finalizes it; a match in `keyfile.json` deletes a stale `.new`). Then: any other open session is locked first (its before-lock backup runs); the database is opened (and migrated when its schema is older) with the sheet's key BEFORE anything is re-encrypted (a failed open or upgrade changes no key file; see Unlock under Private updates for `schema_too_new` and the automatic rollback); only then is the vault rekeyed to the new password and the same sheet re-sealed, the limiter reset, the after-unlock hook and alert evaluation run, and a new session starts. If the rekey fails, nothing changes and the vault is locked again.
- **New sheet** (Settings): `POST /api/recovery` re-checks the password and returns new groups, **pending** in server memory (public key + KDF parameters, never the code) for 30 minutes; any lock or a new session drops it. The pending sheet also holds K' = Argon2id(the password just checked, a **new** salt), wiped on any lock, expiry, new session, a newer pending sheet or Done (a password change in between derives it again from the new password). `POST /api/recovery/activate` (Done) **rotates the vault key**: it records the new public key in the database, writes `keyfile.json.new` (new salt + the new block sealed to K' and MAC'd with it, the old public key retired), `PRAGMA rekey` K → K', renames, reopens — the same crash-safe path as change password; the session stays. Until then the old sheet keeps working. Afterwards the old sheet's block, wherever a copy of it survives (backups, `pre-migrate-*`, `pre-restore-*`), unseals only the old K, which opens nothing current: the old sheet answers `outdated` and is revoked for the live vault and every later backup. A crash before the rekey leaves the old sheet active (the next unlock drops `keyfile.json.new`); a crash after it is finished by the next unlock or recover (password or new sheet), and the old sheet answers `outdated`. A failure after the rekey (the rename is retried a few times on a Windows sharing violation) answers 500 `finish_on_unlock` and leaves FinTrack locked; Settings tells the user to unlock and check which sheet is shown, keeping both until then. If the password change's re-derive of the pending K' fails, the pending sheet is dropped (Done answers `pending_expired`); Done also refuses any pending key that isn't 32 non-zero bytes. An expired pending sheet is dropped (key wiped) on the next request, not only at Done.
- **Restore**: in-app with a valid current sheet, the restored database is rekeyed (still in staging) to a fresh K' = Argon2id(the backup's password, a new salt), K' is sealed to the *current* sheet (so the sheet in the drawer keeps working) and its public key recorded in the restored database; the backup's own sheets are retired (they answer `outdated` when the install's KDF salt matches). So neither the backup's sheets nor the backup's keyfile open the restored live vault; the backup's password still does (it is the password from then on). At setup, or without a valid current sheet, the backup's block is kept if its MAC checks out under the backup's key and its public key is on record in the backup's database, else dropped; a kept block is marked `confirmed:false` (status `unconfirmed`), since it may be the lost sheet, so Settings, Home and the restore toast ask for a new sheet. A backup's database copy and keyfile must match: keyfile.json is read before and after the copy, and a copy made while it changed (Done or a password change in between) is made again once, else the backup fails with 409 `backup_retry`. The set-aside `pre-restore-*` copy keeps its keyfile unchanged. A backup carries its block, so an old sheet + an old backup opens **that backup file** (accepted, documented; the password of that time does too) — never the live vault or a backup made after the sheet was replaced.

## Routes
| Method | Path | Body | Response |
|---|---|---|---|
| POST | /api/auth/recover/check | `{code}` (≤128) | 200 `{ok:true, sheet}`. Does not reset the limiter. |
| POST | /api/auth/recover | `{code, new_password}` (≤128 / ≤4096) | 200 `{ok:true, session_token}` + cookie |
| GET | /api/recovery | – (session) | `{status: "none"|"active"|"unconfirmed"|"stale", sheet, created_at}`; `stale` = the MAC fails, the public key isn't the one on record in the database, or `for_salt` isn't the keyfile's salt |
| POST | /api/recovery | `{current_password}` (session) | 200 `{pending_id, sheet, created_at, groups}`; 422 `password_required`; 401 `wrong_password` + `tries_left` (counted); 429 |
| POST | /api/recovery/activate | `{pending_id}` (session) | 200 RecoveryStatus; 409 `pending_expired`; 500 `finish_on_unlock` (locked; the next unlock finishes it) |
| POST | /api/recovery/confirm | `{sheet}` (`NNN-NNN`, session) | 200 RecoveryStatus; 409 `sheet_changed` |

Errors of `check` and `recover` (every body `{detail, code, …}`): 409 `not_initialized`; 422 `bad_format`; 422 `typo` + `groups`; 422 `weak_password` (recover only; not counted); 409 `no_recovery`; 401 `no_match` + `tries_left` + `active_sheet` (counted); 409 `outdated` + `active_sheet` (an older sheet of this vault; not counted); 409 `unusable` + `active_sheet` (the sheet matches but its sealed key doesn't open the data; not counted); 429 `rate_limited` + `retry_after`. Unknown fields, wrong types and over-long values are 422 without echoing the input.

## Security
- The code appears only in the setup / new-sheet responses and the check / recover requests (POST, JSON, `no-store`) and in React state: never in a URL, storage, the clipboard, logs, exception messages or on disk. Tests grep captured DEBUG logs and every error body for the code, its groups and its 30 data digits. Python strings can't be wiped; byte buffers (seed, key, data digits) are.
- Brute force: ~99.7 bits × Argon2id offline; online, the shared limiter, with Argon2 serialized under the vault lock. Typos and format errors cost no Argon2 run. `outdated` and `unusable` aren't wrong tries (the shared count is untouched), but they cost an Argon2 run, so they have their own soft limit: more than 10 a minute → 429 `rate_limited` + `retry_after` (checked before Argon2, for any code).
- Threat model: sheet + computer opens FinTrack (by design); the computer alone is no easier to open than before. Making a new sheet rotates the vault key, so a replaced (lost) sheet no longer opens the live vault or any later backup, even with an old keyfile copy. An old sheet + a backup made while it was active opens **that backup file** (accepted: keep old backups as safe as the sheet, and destroy a replaced sheet). Write access to `data/` can't plant a sheet (MAC) or put an older one back (MAC under the rotated key + the public key on record in the database). Malware on the computer is out of scope. The sheet number and `retry_after`/`tries_left` before login are harmless.
- `SUPPORT_CONTACT` (≤60 characters, default empty) names who to call in the unlock screen's messages.

# Release 3.6: Cash forecast calendar

The Recurring page becomes a month calendar of expected money in and out with the projected checking balance per day, up to 12 months ahead. Plan: the recurring design notes' MAPPING.md and BILLS-ADDENDUM.md (not in the repo; the addendum wins where they differ). The server computes everything (`services/calendar.py`); the client's `project()` is no longer used.

## Schema v6 (migration, one transaction, pre-migration safety copy as in 3.5)
- `recurring_items`: `reminder_days INTEGER NOT NULL DEFAULT 0` (0, 1 or 3), `start_date DATE` (no occurrence before it; NULL = no limit), `category_id TEXT REFERENCES categories(id) ON DELETE SET NULL` (the bill's budget category).
- `recurring_overrides` (`id`, `recurring_id` → `recurring_items` ON DELETE CASCADE, `base_date`, `moved_to`, `skipped`, `paid`, `paid_amount_cents` (the item's sign), `created_at`; UNIQUE(`recurring_id`, `base_date`); index on `recurring_id`).
- `alert_settings` rows `('low_ahead', 1, 7)` and `('reminder', 1, NULL)`.
- `app_settings.recurring_category_backfill = '1'`: on the next unlock (and on the first `GET /api/recurring` or calendar load) every **active** item without a category gets one from its past transactions (rule below), then the flag is deleted, so a category cleared later is never refilled. Fresh vaults have no flag.
- Backups made from now on have `schema_version` 6; older builds refuse them and v6 vaults (`SchemaTooNew`).

## Cadence "once"
`Cadence` gains `once` (manual items only; `CADENCES`/`classify` are unchanged, `MANUAL_CADENCES = CADENCES + ("once",)`). It has exactly one occurrence, `next_date`; its period (for the carry window) is 14 days. Detection never updates a `once` item (a matching group is skipped: no new suggestion, `next_date` untouched). The legacy `GET /api/forecast` shows it once.

## Occurrences (single source of truth: `services/calendar._generate`)
- **Base dates** (`recurring.rule_dates`) go both ways from `next_date`: weeks in whole steps, months with the anchor day (`month_days`, 31 = the month's last day), semimonthly = the two anchor days of every month (default 1st and 15th) plus `next_date`. None before `start_date`. `POST /api/recurring` sets `start_date = next_date`; a PATCH that moves `next_date` before `start_date` moves `start_date` with it.
- **Key** `r{item}:{base}` (plans: `p:{category}:{date}`); opaque to the client.
- **Overrides**: `moved_to` changes the effective date; overrides whose base is no longer on the rule are ignored (kept, harmless).
- **Consumed bases**: a base in [today, `next_date`) exists only when paid (detection already moved past it).
- **Matching** a transaction: same merchant key and sign; the item's account, or any visible account when it has none; transfers and pending rows count; |txn date − effective date| ≤ 3 (weekly), 5 (biweekly, semimonthly), 7 (otherwise); |amount| between 0.5× and 2× the expected one. Occurrences are assigned in date order, each transaction once, nearest date first, then nearest amount, then lowest id.
- **Status** of an occurrence dated d (after moves): skipped override → `skipped`; paid override → `paid` (`actual.by = "you"`, amount = `paid_amount` or the expected one); a matching transaction → `paid` (`by = "match"`, its amount, date and id; also when d is still ahead: paid early); d ≥ today → `upcoming`; else `past` when the item isn't trackable (manual and never seen) or a later payment was already seen (`base ≤ last_seen_date`); else `pending` (≤ 3 days late) or `late`.
- **Carry**: today's unmatched occurrence and pending/late ones with today − d ≤ min(14, period) are counted **tomorrow** (`carried: true`) when the series is counted. Mark as paid or Skip stops it.
- `bill_occurrences(session, today, start, end, account_ids=None) → {category_id: [Occ]}` (active items with a category and a negative amount; `account_ids` narrows them to items on those accounts or on no account; the Budget screen passes its accounts plus every visible credit card) and `income_occurrences(...) → [Occ]` (positive amounts, any category) return the same occurrences for the Budget screen (effective date in [start, end], no carry). A `bills` target's "needed" should count every status but `skipped`, `paid` at `actual_cents`.

## `GET /api/forecast/calendar?from=&to=` → `ForecastCalendar`
- Defaults: `from` = today, `to` = today + 35. 422: `from > to` ("from must be on or before to"), `from` < today − 62 ("from is too far back"), `to` > today + 400 ("to is too far ahead").
- Account = the forecast account (unchanged rules). None: `account: null`, `balance: 0`, empty `days`/`occurrences`/`months`/`series`, `first_dip: null`.
- Calendar items = **active** items with no account, the forecast account, or a credit account (`kind: "card"`, never counted). Items on other bank accounts are left out (the page lists them under "All repeating items").
- `ForecastCalendar` = `{account {id, name, mask, institution_name}, today, from, to, horizon_end (last day of month(today)+12), balance, threshold (the "low" value), daily_spend, include_daily, low_ahead {enabled, days}, reminders_enabled, days, occurrences, months, first_dip, series}`.
- `days[]`, one per date in [from, to]: `balance` null before today, today's balance on today, then `prev − daily (if include_daily) + Σ counted occurrences with counted_on = d`; `below = balance < threshold`. The projection always starts today, whatever `from` is.
- `occurrences[]`: every occurrence whose effective date is in [from, to], plus carried ones; sorted by date, money in before money out, name. Fields as MAPPING §5.6 plus `category_id`. `counted` = series counted (`include_in_forecast`, kind in/out) and status upcoming (d > today) or carried; plans are counted unless excluded. `movable` = not paid/skipped/past (plans never). `next_in_series` = the series' next scheduled base (first rule date ≥ max(today, `next_date`)) and cadence not once/semimonthly. `override.paid_amount` is positive; `actual.amount` is signed.
- **Plans**: every non-hidden spending category with a **by_date** target dated in [today, to] (other target kinds, including a future `bills` kind, are ignored): `kind: "plan"`, name = category, amount = −target, `counted_on` = max(date, tomorrow), read-only.
- `months[]`: month(today) … month(to), over days ≥ today and ≤ to: `{month, low, low_date (first lowest day), end (null when the month ends after to), end_date, first_dip}`. `first_dip` = `{date, balance, cause_key}`: the first below-limit day; `cause_key` = the most negative counted outflow on that day (null when only everyday spending). Top-level `first_dip`: the first in [max(from, today), to].
- `series[]`: calendar items (`id: "r{id}"`) then plans (`"p:{category}"`, `source: "spending"`, `cadence: "once"`), with `counted`, `reminder_days`, `can_move_all`, `category_id`, `account {id, name, mask, category}`.

## Forecast settings
`GET /api/forecast/settings` and `PATCH /api/forecast/settings` `{include_daily?, excluded_plans?}` → `{include_daily, excluded_plans}`. Stored as `app_settings.forecast_include_daily` ('1'/'0', default on) and `forecast_excluded_plans` (JSON category ids, at most 200 of ≤ 64 chars; duplicates dropped; unknown category → 422 "unknown category"; explicit null → 422; ids of later-deleted categories are dropped when read).

## Recurring changes
- `RecurringItem` gains `reminder_days`, `start_date`, `anchor_days` (read-only, parsed `month_days`), `category_id`, `category_name`.
- `POST /api/recurring` also takes `reminder_days` (0/1/3, default 0), `cadence: "once"` and `category_id`. **Omitting** `category_id` guesses it from the merchant's history; `null` means none. `PATCH` takes `reminder_days` (null → 422 "reminder_days must not be null") and `category_id` (null clears). A category must exist and not be transfer-kind, else 422 "unknown category". Deleting a category sets its bills' `category_id` to null.
- **Category from history** (detection, confirming a suggestion — `PATCH status: active` from `suggested` without a `category_id` and while it has none — and the v6 backfill): the most common effective category of the item's past transactions (detection's input: last 400 days, non-pending, non-transfer, visible accounts, same merchant key/sign/account), ignoring OTHER/uncategorized, hidden, unknown and transfer-kind categories; ties go to the most recent; none left → null.
- `POST /api/recurring/category-guess` `{name, amount_sign?: 1|-1, account_id?}` → `{category_id, category_name}`: the Add dialog's pre-fill (merchant key of `name` equal to, or a leading word run of, the transaction's; same sign when `amount_sign` is given). A POST body so typed bill names never appear in URLs or access logs.
- `GET /api/recurring/candidates` → `RecurringCandidate[]` for `suggested` items, by `count` desc then name: `paid_with` (`card` = credit account; `checking` = no account or a checking bank account; else `other`), `on_calendar`, `day_of_month` (monthly/quarterly/yearly anchor, 31 = last day), `weekday` (weekly/biweekly, 0 = Sunday), `months_seen` (YYYY-MM, last 4, oldest first), `count` (distinct days seen), `last_date`.
- `GET /api/recurring/{id}/snapshot` → `{item: RecurringItem + {merchant_key, created_at}, overrides: OccurrenceOverride[]}`. `POST /api/recurring/restore` (body = a snapshot; `account_name`/`category_name` accepted and ignored) validates everything first, then overwrites the item with that id and replaces its overrides (200), or inserts it with that id (201). 422: id outside 1..2⁶³−1, amount 0, bad cadence/status/source, `anchor_days` not fitting the cadence (1 day for monthly/quarterly/yearly, 2 for semimonthly, none otherwise), `merchant_key` empty or > 120, > 500 overrides, a repeated `base_date`, `paid_amount` without `paid`. A missing account or category becomes null.
- `POST /api/recurring/{id}/reschedule` `{from_base, to}` ("Move all future ones too") → `{item, undo: snapshot before}`: sets `next_date = to`, re-anchors `month_days` on `to` (keeps 31 when the old anchor was 31 and `to` is a month end), lowers `start_date` to `to` if needed, deletes overrides with base ≥ `from_base`. 404 unknown item; 422 not active ("Only items on your calendar can be changed."), once/semimonthly ("This one can only be moved one at a time. Use Edit details to change its usual day."), `from_base` not the next scheduled base ("Only the next one can move every future one."), `to` < today ("Pick today or a later date."), `to` > horizon_end ("Pick a date within the next 12 months.").
- `PUT /api/recurring/{id}/occurrences/{base_date}` `{moved_to = null, skipped = false, paid = false, paid_amount = null}` (the whole row) → `{override, previous}`; all defaults delete the row; `moved_to == base_date` is stored as null. 404 unknown item; 422 not active, base not a date of the current rule ("That date isn't one of {name}'s dates."), a **new** `moved_to` before today or after horizon_end (an unchanged one may be past, e.g. marking a moved, now late, one paid), `paid_amount` ≤ 0 or over the money maximum, `paid_amount` without `paid`. `DELETE` the same path → 204, idempotent (404 only for an unknown item). Undo = `PUT previous` or `DELETE`.

## Alerts
- Order: low, big, budget, due, newrec, price, conn, income, **low_ahead**, **reminder**.
- `low_ahead` (days, default 7, whole days 1–31 — `PUT /api/alerts/settings/low_ahead` 422 otherwise; severity warn): the calendar over [today, today+N] with the `low` value (whether or not `low` is on); fires for the first dip unless checking is already below the limit today (then `low` covers it). Dedupe `low_ahead:{account}:{dip date}`. "Total Checking may drop below $2,000 on Oct 8" / "Projected $1,640 after Rocket Mortgage. Open the Cash forecast to move a bill or money." `data` = `{account_id, date, balance_cents, threshold_cents, cause_key}`.
- `reminder` (no value, severity accent): calendar occurrences with `reminder_days` > 0, status upcoming/pending (or carried), today ∈ [d − N, d]. Dedupe `reminder:{item}:{base}:{d}` (a moved one reminds again). "Rocket Mortgage is due in 3 days/tomorrow/today" (income: "is expected …") / "$2,210.44 from Total Checking on Thu, Oct 8." (income "to …", card "on {card}"). `data` = `{recurring_id, base_date, date, amount_cents}`.
- `evaluate(..., only=keys)`; both run after unlock and sync as before, **plus** `automation.alerts_tick` (hourly, only while unlocked, `only=("low_ahead", "reminder")`, type-only logging) in the lifespan loop. A failure in either is logged and doesn't stop the other alerts.
- **No email** (owner decision): reminders are in-app only (Alerts, Home). No SMTP, no new network destination.

## Home
`GET /api/home` is unchanged (its `forecast` is still the legacy payload). The client switches Home's bills card and low-balance alert to `GET /api/forecast/calendar?from=today&to=today+35`; the server's `low_ahead` alert is the future source for Home's `low_balance`.

## Security
Session + CSRF as everywhere; bodies are StrictModel3 (unknown fields and NaN/Infinity 422); ids bounded to 2⁶³−1. Restore validates before writing and only touches the one id and its overrides. Overrides cascade with their item and are in the encrypted backups. Nothing financial in URLs or localStorage (UI preferences only: week start, show balances, view; `?d=` carries a date). Alert bodies (bill names, amounts) stay in the encrypted DB; logs carry exception types only.

# App window states (D4)

Design: the appwin design notes (README + MAPPING.md, the contract; not in the repo). Backend: `presence.py`, `security.Vault` (session binding), `routers/app.py`, `routers/auth.py`.

## Windows (tabs) and presence
- Every page sends `X-FinTrack-Tab: <crypto.randomUUID()>` (kept in its sessionStorage). Valid ids match `^[A-Za-z0-9-]{8,64}$`; anything else counts as no id. **Never an auth factor**: it only says which window is talking.
- Duplicated tabs (the browser copies sessionStorage, so the id and the session token): before its first status request each page posts `{type:"hello", tab, nonce}` on `BroadcastChannel('fintrack')` and waits 250 ms; a live page with the same id answers `hello-taken`, and the newcomer takes a new id and drops the copied token (it then opens at "already open" or the password, never inside the other window's session).
- The frontend calls `POST /api/auth/lock` only from the open app (Lock button, idle timer), never from the password or "already open" screens: a lock without a valid session locks the vault for every window and clears the shared cookie.
- `presence.Presence` (own lock) keeps `last_seen[tab]` (monotonic) from `GET /api/auth/status` and `POST /api/app/alive`, at most 32 tabs (the oldest is dropped, but never the tab holding the session or one whose close is pending, so a flood of made-up ids can't cancel a close-lock or hide the open window). A tab is **present** if seen within `PRESENCE_TTL` = 100 s and not closing.
- Setup, unlock, change-password, restore and recover **bind** the session to the calling tab (and mark it seen). Change-password without a tab header keeps the current binding.
- Replacing a session from another tab (unlock, recover; also a caller with no tab id) keeps `sha256(old token)`: requests with that token get 401 `{"detail":"moved"}` instead of `locked`. Decided by the token alone (the browser's windows share the cookie). The same tab re-unlocking, change-password and in-app restore (the caller held the session) are not moves. Every lock clears the marker.
- `open_elsewhere` (status, alive) = the vault is unlocked, the caller has no valid session (none, or a moved one), the session is bound to a tab that is not the caller's, and that tab is present. A session made without a tab id is never "elsewhere".
- `lock_reason`: `moved` for a caller holding a moved token; null while the vault is unlocked; otherwise why it last locked: `idle` (server auto-lock, or `POST /api/auth/lock {"reason":"idle"}`), `manual` (lock button / no body), `closed` (the bound window closed), `shutdown` (server exit), null (not since this server started, or an internal lock: restore, recover, a failed rekey). Locking an already locked vault doesn't change it.
- Unauthenticated disclosures (status, alive, health): whether FinTrack is unlocked in another window, whether any window is open, the last lock reason. Low sensitivity; same-origin only (Host allowlist, no CORS; the custom header needs a preflight cross-origin).

## Routes (`routers/app.py`; every POST needs the CSRF header and a JSON body or none)
| Method | Path | Auth | Request | Response |
|---|---|---|---|---|
| POST | /api/app/alive | none (not activity) | `X-FinTrack-Tab` required | 200 `{open_elsewhere, unlocked, lock_reason, boot_id}`; 400 `{"detail":"invalid tab"}`. The self-exit heartbeat; marks the tab seen, cancels its pending close. The idle auto-lock still runs first |
| POST | /api/app/closing | none | `X-FinTrack-Tab` required (fetch keepalive on pagehide, not persisted) | 204. If the tab holds the session: lock `closed` (before-lock backup first) after `CLOSE_GRACE` = 8 s unless the same tab calls status/alive first (a reload). Any other tab is forgotten. Can only lock. 400 `invalid tab` |
| POST | /api/app/handoff | session (cookie + token) | `{"to_tab"}` (unknown fields 422) | 200 `{session_token}`: the token rotates (cookie unchanged), the session is bound to `to_tab`, the old token then gets `moved`, a pending new recovery sheet is dropped. 400 `invalid tab` (malformed or the session's own tab); 401 `locked`/`moved`; 409 `{"detail":"window not found"}` (`to_tab` not present) |

"Open here": the new window asks the window holding the session over `BroadcastChannel('fintrack')`; that window calls handoff and posts the new token back. The asking window takes only an answer to its current request (one-time nonce) carrying a 1-128 character token, and opens once. Nothing moves a session without the current token (no passwordless takeover by a local process); the fallback is the password.

## Close = lock
`closing` records a deadline; a timer (`loop.call_later`, 8.25 s) runs `finish_close` in a thread, and the 30 s idle loop sweeps due closes as a backstop. `Vault.lock_if_bound(tab, "closed")` re-checks the binding under the vault lock. `closing` notes when it arrived: if the same tab sent status/alive after that (a reload's status overtaking the old page's beacon), no close starts. The frontend sends it without `X-FinTrack-Session` (CSRF and tab headers only).
- Lost beacon (browser killed, crash), **packaged start only** (`exit_when_unused_seconds` > 0): the 30 s loop also locks `closed` when the session's tab hasn't sent status/alive for over `PRESENCE_TTL` (100 s). Not for the owner's `run.py` (0): a background tab's throttled timers must not lock the user out; the idle auto-lock still applies.

## Persisted wrong-try count
`data/limiter.json` = `{"version":1, "failures":n, "locked_until":<unix time or 0>}`, written (atomically, best effort) on every counted failure and removed on reset; not written before the vault folder exists. Untrusted on load: a malformed file is ignored; failures are capped at 21; a loaded wait is at most the lockout that count gives (a clock moved back can't extend it). Because the count survives restarts, a local process that keeps sending wrong passwords can keep the lockout going across restarts too (inherent to a loopback server any local program can reach; it can't open the vault).

---

# Release 3.6: Budget for a beginner (phases 1 and 2)

The Spending page becomes **Budget** (nav label; the route stays `/spending`) with two views of the same envelope budget: a **Simple** view for someone new to budgeting (default) and the **Detailed** view, which is the Release 3.1 screen unchanged. The choice is the UI pref `budget.view` (`simple` | `detailed`). Design: the budget design notes (not in the repo; the "OWNER ANSWERS" in their `MAPPING.md` are final). No migration: everything new is stored in `app_settings`. The Release 2.1, 3 and 3.1 rules still apply.

## Expected income and the income switch
- `app_settings.budget_income` = `{"mode": "expected"|"off", "months": {"YYYY-MM": cents}}`. Missing or unreadable = mode `off`, no months. Stored months are pruned to [T−3, T+12] on every write; switching the mode keeps them.
- **received(m)** = Σ positive ledger lines of an income- or transfer-kind category (split-aware, pending included, lines flagged `is_transfer` excluded) on the budget accounts in m. Transfer kind counts because pay sent by Zelle or Venmo is often categorized "Transfer in" without being a matched transfer between the user's accounts; otherwise it would be in the cash and still pending (counted twice). Refunds (spending kinds) never count.
- **suggested** = the average of received over the last 3 complete months, skipping months with none, rounded down to $10; null when none had income (or it rounds to $0).
- **expected** = `months[T]` when set, else suggested. **pending** = max(0, expected − received(T)), only in mode `expected` (0 in mode `off`).
- **Ready to Assign += pending**, so the over-assign rule, `fund-targets`, the `income` alert and every view use it. **Mode `off` gives exactly the Release 3.1 numbers.**
- **Money for {Month}** (`month_money`) = Ready to Assign + Σ assigned in the month. "Planned" = `assigned`; "Not planned yet" = max(0, Ready to Assign).
- The guided setup turns mode `expected` on. The Simple view's "Change amount" editor (labelled "How it's counted" while off) and the "Expected income and savings" tile offer the plain choice "Count paychecks I expect this month" / "Only money that's already in my accounts".

## `GET /api/budgets` (additive)
`month_money`, `income` = `{mode, expected, expected_override, suggested, received, pending, next, event}`, `savings_category` (id or null), `setup_needed`, `bills_outside` and `bills_available` (phase 2, below), `ready_pending` (today's pending, on every month's view, since Ready to Assign is global), `seed_category` (setup's "money you already had" row: the savings category in the month setup seeded, else null; it is not a plan, so Quick fill, "Assign what you did last month" and `suggested` leave it out).
- For the viewed month: `received` is that month's and `expected_override` = `months[m]`; `pending`, `next` and `event` only for today's month (0 / null otherwise).
- `next` = `{date, name, amount}` of the next expected paycheck (phase 2: from income occurrences, below).
- `event` (mode `expected`, expected not null), derived and never stored (a PUT of `income_expected` resolves it):
  - `more` when received − expected ≥ $1: `{kind, amount, date, name, expected, received}` (date and name of this month's newest deposit counted in received).
  - `less` when expected − received ≥ $1 and no paycheck is still expected this month: `{kind, amount, date: null, name: null, expected, received, reason: "short"|"missing" (nothing came in), can_use_unplanned: Ready to Assign ≥ amount}`. "Still expected" = an active income item with `next_date` in [today, month end]; with no active income item at all, pay is assumed in by the 20th, so a short month is called out from the 21st.
- `setup_needed` = no `budgets` rows at all and `app_settings.budget_setup_done` unset.
- `savings_category` = `app_settings.budget_savings_category` when it is a spending category (it is never looked up by name).

## `PUT /api/budgets/{month}` (additive)
`BudgetSave` += `income_expected: number ≥ 0 | null` (null = back to the suggestion) and `accept_received: bool`. One transaction with `assigned`/`removed`; everything is validated before anything is written.
- Only today's month (422 "The month's amount can only be changed for this month."; past months keep their read-only 422) and only in mode `expected` (422 "The month's amount follows the money in your accounts. Choose “Count paychecks I expect this month” to change it.").
- Below what came in: 422 "$Y has already come in this month, so pick at least $Z." (Z = the month's amount with expected = received).
- A save that lowers pending and leaves Ready to Assign below 0 and lower than before: 422 "Your plans add up to $P. Lower some plans first, or pick at least $P." Other over-assign refusals keep the Release 2.1 text.
- `restore: bool` (Undo putting back an earlier state): only with `income_expected` and/or `assigned`, never with `accept_received`; it skips only the "already came in" check, so an earlier amount below what came in can come back (every other rule applies).
- `accept_received` ("Leave it for next month"): `income_expected` must equal received, no plan may rise and nothing may be removed (422 otherwise). Ready to Assign may go negative only by the income that didn't come in: the save's plan changes must still leave Ready to Assign (counted with the pending before the save) ≥ min(Ready to Assign before, 0).
- The client maps a new month amount M' to `income_expected = max(expected ?? received, received) + (M' − M)`. Undo = PUT the previous `{assigned, income_expected}`.

## New routes (session + `X-FinTrack`; unknown fields, NaN and Infinity → 422)
- `PUT /api/budgets/settings` `{income_mode?: "expected"|"off", savings_category?: string|null}` → 200 BudgetMonth (today's month). 422 for an unknown or non-spending category, or `income_mode: null`.
- `GET /api/budgets/setup` → `{month, needed, income: {suggested, received}, rows, bills (phase 2), existing_money, owed_beyond_cash}`. `rows` in the order groceries, gas, eating_out, bills, fun, other: `{key, label, hint, categories (the names it fills), average, chips, suggested}`. Averages are spending on the budget accounts over the last 3 complete months (only months on or after the first month with spending; null without any). Chips = the average ×0.75 / ×1 / ×1.25 rounded to $50 (at least $50, duplicates dropped) when it is ≥ $10, else the design's. `existing_money` = max(0, E) and `owed_beyond_cash` = max(0, −E), E = cash + this month's spending in the plan's categories − received. When cards owe more than the accounts hold, the setup card says "Your cards owe $D more than your accounts hold, so plan at most $L." (L = income − D).
- `POST /api/budgets/setup` `{answers: {groceries|gas|eating_out|bills|fun|other: number ≥ 0}, income_expected, keep_existing_in_savings = true, skip = false}` → 201 BudgetMonth. 409 "Your budget is already set up." unless `setup_needed`; 422 "Lower an amount to continue." when Σ answers > income − `owed_beyond_cash` (not for skip; with debt the detail starts with the setup card's note); 422 when income < received. One transaction:
  - "Groceries", "Eating out", "Gas" and "Emergency savings" are custom spending categories. A same-name spending category (ignoring case) is reused and un-hidden; a same-name category of another kind → 422.
  - Groups only when there are none: Bills (RENT_AND_UTILITIES, GENERAL_SERVICES), Everyday (Groceries, Eating out, Gas, FOOD_AND_DRINK, TRANSPORTATION, GENERAL_MERCHANDISE, PERSONAL_CARE, MEDICAL), Fun (ENTERTAINMENT, TRAVEL), Other and saving (OTHER, HOME_IMPROVEMENT, BANK_FEES, GOVERNMENT_AND_NON_PROFIT, Emergency savings, any other visible spending category). Only ungrouped categories join. When groups exist, Groceries and Eating out join FOOD_AND_DRINK's group and Gas joins TRANSPORTATION's (only if they are ungrouped).
  - Plans for today's month: the three custom rows get their answer; Bills, Fun and Other are split across their visible categories by the 3-month average (a category with spending only this month is weighted by that; floors, the leftover cents go to the largest; with no weights the whole amount goes to the bucket's first category). Members = the custom rows, Emergency savings (plan 0) and every bucket category with spending (in the window or this month) or a plan. **Phase 1:** FOOD_AND_DRINK and TRANSPORTATION stay out of the plan; their spending shows under "Spending without a plan" until it is moved to the new categories.
  - `skip`: the same categories and groups, with every plan = this month's spending so far.
  - Mode `expected`, `months[T]` = income, `budget_savings_category`, `budget_setup_done = "1"`, and `budget_setup_seed = {"month": T−1, "category"}` when the T−1 savings row is written (cleared otherwise).
  - `keep_existing_in_savings`: extra = Money for {Month} − income (after the plans). When extra > 0, `Budget(T−1, savings, extra + the savings category's spending in T−1)` is written, so its carryover into T is exactly extra and Money for {Month} equals the income. This is the only server-side write to a past month, made only here and only when there were no budget rows.
- `POST /api/budgets/{month}/categories` `{name (1–60), group_id|null, plan ≥ 0}` → 201 `{category, month}`: a custom spending category with that plan, in one transaction. 409 for a duplicate name; 422 for an unknown group, an empty name, a past month, or "Only $X isn't planned yet. Use a smaller amount, or change the month's amount." (X = max(0, Ready to Assign)). Undo = `DELETE /api/categories/{id}`.

## Alerts
- `income` in mode `expected` with an expected amount counts only this sync's deposits (income or transfer kind, not `is_transfer`) dated in today's month, and fires only when their contribution to the surplus — max(0, received − expected) − max(0, received − new − expected) — is ≥ $1: "{contribution} more than expected came in" / "{merchant} on {account}. It's under Not planned yet." (or "N deposits. …"). Otherwise unchanged ("$X new to assign").
- `budget` copy: "{name} went over its plan" / "{name} has used N% of its plan" (the body, "$X of $Y spent", and the dedupe keys are unchanged).

## Home
`budget` leaves the savings category out of `planned`, `spent`, `left` and `has_budget`.

## Simple view (frontend)
- Header "Budget" / "{Month Year} · N days left". While `setup_needed`, the first-time setup card replaces the page.
- Blocks: "How this page works" (pref `budget.helpHidden`); "{Month} is here. Start with {previous}'s plans?" when the month has no rows (`suggested`); the three tiles; the income note (Earned more: add it to this month's plan / put it in savings / leave it for now; Paycheck short: use money that isn't planned yet / take it from categories / use savings / leave it for next month); the Cover it banner; the status message (not planned yet / every dollar has a plan / "You've planned $X more than you have. Lower a plan to fix it."); one card per group (hue from the group's position); "Add a group"; "Spending without a plan" ("Add to my plan ($X)" plans what was spent); More options.
- Rows: a plan button (Enter or leaving saves, Esc cancels; a raise past Not planned yet is blocked with "That would plan $X more than {Month}'s $Y. Lower another plan first, or change the month's amount."), Spent, and Left with words (left this month / getting close at 90% / all used / to set aside / "Over by $X" with Cover it). Rows stack under 600px of content.
- More options: Move money, Targets, Show carried-over amounts (pref `budget.showCarry`), Quick fill, Budget accounts, Expected income and savings, Detailed view.
- Every change shows the shared Undo message (`components/UndoToast.tsx`: 12 s, paused while hovered or focused). The Cover it / lower-plans picker is a 400px dialog.
- `?cat=<id>` opens either view at that category; the parameter is then removed.
- The Detailed view's Ready to assign adds "Includes $X still expected this month" when `ready_pending` > 0 (any month). For T−1 it labels the seeded savings row "Money you already had".

## Phase 2 (schema v7)
Bills per category, the `bills` target, income from calendar occurrences, the setup's Bills row and Plaid's detailed categories. Everything about bills and paychecks comes from `services/calendar.py`'s occurrences (`bill_occurrences` / `income_occurrences`), the same ones the Recurring calendar shows, so the two never disagree.

### Schema v7 (migration, one transaction, pre-migration safety copy as in 3.5)
- `ALTER TABLE transactions ADD COLUMN plaid_detailed TEXT` (existing rows NULL). Backups made from now on have `schema_version` 7; older builds refuse them and v7 vaults (`SchemaTooNew`).

### Bills per category (`GET /api/budgets`, additive)
- `EnvelopeLine.bills` = `{total, items: [{key, recurring_id, name, date, amount, status, actual_amount, paid_date}]}` or null when the category has no bill in the viewed month. Bills = occurrences of active money-out recurring items with `category_id` = the category, effective date (after a move) in the month, paid from a budget account, from any visible credit card (even one left out of the budget: a bill charged to a card is still spending in its category; paying the card later is a transfer) or from no account. A bill paid from any other account (savings, a brokerage, an account left out of the budget) isn't budget spending and doesn't count (`spending.bill_account_ids`); the Recurring calendar still shows it. `status` is the calendar's (`upcoming | pending | late | paid | skipped | past`); `amount` is the expected amount and `actual_amount` the paid one (both positive); `paid_date` = the matched transaction's date (the due date when marked paid by hand). **total** = Σ over non-skipped items of the actual amount when paid, else the expected amount; `include_in_forecast` doesn't matter.
- `bills_available` = false for a month that ended more than 62 days before today (the calendar's `PAST_DAYS`): every `bills` is null and `bills_outside` is empty.
- `bills_outside` = `[{category, name, hue, total}]`: visible spending categories with a bill total > 0 this month that aren't in the plan, largest first. The Simple view lists them in "Spending without a plan" with "Your bills here add up to $X this month." ("Add to my plan" plans max(spent, bills)).

### The `bills` target ("Cover my bills")
- `PATCH /api/categories/{id}` `restore: true` (strict boolean, only with `target`, else 422 "Undo needs the target to put back."): Undo putting back an earlier target, so a by-date target's date may be in the past; every other rule applies.
- `TARGET_KINDS` += `bills`: `target_kind = 'bills'`, `target_cents` and `target_date` NULL. `PATCH /api/categories/{id}` `target: {kind: "bills"}` (`amount`/`date` may only echo null): 422 "A bills target has no amount: it follows your bills each month." / "A bills target has no date."; monthly and by-date targets without an amount: 422 "The target amount must be more than $0.". Changing the kind away from spending clears it, like any target.
- In a budget month `target.amount` = that month's bill total (null when `bills_available` is false) and **needed = max(0, bill total − assigned)** (carryover ignored, like a monthly target). `/api/categories` returns `amount: null`. `fund-targets` funds it; the calendar's `plans` stay by-date targets only.

### Income from occurrences
- `income.next` = the first `upcoming` income occurrence dated today or later (moves and skips applied), from active money-in items on a budget account or on no account; it looks 62 days ahead.
- "Still expected this month" (the `less` event is held back) = such an occurrence in today's month that is `upcoming` or `pending` (a few days late). With no active income item, the phase 1 fallback (the 21st) is unchanged. The `less` event's `date`/`name` are those of this month's latest `late`/`past` paycheck occurrence (null when there is none).
- **received(T)**, only with expected income on (`mode = "expected"`; with it off every number is unchanged), += deposits matched to a paycheck occurrence (by the calendar's matching) that aren't already counted: dated in T, on a budget account, not `is_transfer`, and not a transaction with any part counted by the income-kind sum (deduped by transaction id). E.g. a paycheck Plaid filed under Other. Earlier months' received (and so `suggested`) are unchanged.

### Setup
- `GET /api/budgets/setup` `bills` = `{total, count, by_category, extra, extra_categories}` from this month's bill occurrences (budget accounts and cards, as above) in visible spending categories (null without any). Every such category (except the custom rows, Emergency savings, FOOD_AND_DRINK and TRANSPORTATION) joins the **Bills** row and leaves the Fun/Other rows. A category that joined from elsewhere (a streaming bill in Entertainment) brings its usual other spending: **extra** = max(0, its 3-month average − this month's bill total) (an estimate; the history doesn't say which payments were the bill), named in `extra_categories`. With bills, the Bills row's chips are total + extra (whole dollars, rounded up) and the next two $50 steps above it, and `suggested` is total + extra. The screen adds "The suggestion also covers about $X of other spending in {names}."
- `POST /api/budgets/setup`: the Bills answer gives each category its bill total first, then each category that joined for a bill its `extra`, and splits the rest by the 3-month average (an answer below the bills is split by bill total; one below bills + extras gives what's left after the bills to the extras by size). So Entertainment's plan is its bill + its other spending and it isn't "Over" on day one; its `bills` target stays (needed 0). Categories with bills are members, join the Bills group when groups are created, and get a `bills` target unless they already have a target.
- `income` = `{suggested, received, expected_more, start}`: `received` counts matched deposits (the setup switches expected income on; the "pick at least" check uses it too); `expected_more` = paycheck occurrences this month still `upcoming`/`pending`; **start** (what the income field starts from) = received + expected_more when a paycheck (not skipped) is on the calendar this month, else max(suggested, received). With the pay schedule, a two-paycheck month after a three-paycheck one isn't suggested the average, so "Paycheck was short" shows right after setup only when the confirmed amount is more than received + still expected.
- When the setup creates the groups it adds a fifth, **Income**, holding the visible income categories with money in (positive, not a transfer) since January 1, so paychecks aren't "Needs a category" once groups exist. A vault that already had groups is left as it was. The budget's `groups` leave out a group whose categories are all income, fixed or transfer ones (an empty group still shows).
- The setup screen counts what the setup's automatic sorting (below) will move: those transactions count as Groceries / Eating out / Gas spending in `existing_money` / `owed_beyond_cash` (so the amount it promises to keep in Emergency savings is the amount the setup keeps), and in those rows' `average` and chips, but the averages only when every averaged month has transactions with a detailed code (history synced before schema v7 has none, and would average too little).

### Sorting into Groceries, Eating out and Gas (`services/automap.py`)
- Schema v7: `transactions.plaid_detailed TEXT` (nullable). Sync stores Plaid's `personal_finance_category.detailed` (uppercase `[A-Z][A-Z0-9_]{0,63}`, anything else NULL) on added and modified rows. Rows synced before v7 (and manual ones) stay NULL; nothing is backfilled.
- The setup records its three categories in `app_settings.budget_auto_map` = `{"groceries": id, "eating_out": id, "gas": id}` and re-applies the rules. Built-in mapping: FOOD_AND_DRINK_GROCERIES → groceries; FOOD_AND_DRINK_RESTAURANT, _FAST_FOOD, _COFFEE → eating_out; TRANSPORTATION_GAS → gas (codes from Plaid's taxonomy; liquor stores, vending machines and "other food and drink" are left alone). A row applies only while its category exists and is a spending category; without the setting the mapping is off.
- Precedence: hand-set (`user`, `user_bulk`) > the first matching user category rule > the mapping > Plaid's primary. A mapped transaction gets **`category_source = "auto"`**: still automatic (rule runs, "back to automatic", category deletes and later syncs recompute it; Undo accepts it), but settled, so it never counts as "Needs a category". The Transactions side panel's "Category from" says "Sorted automatically" (the category shows right above it).
- `PATCH /api/categories/{id}` that hides/unhides a category or changes its kind re-applies the rules in the same commit when the category is one of `budget_auto_map`'s: hidden or no longer spending, its sorted transactions go back to the bank's category (automatic, so "Needs a category" sees them); shown again or back to spending, they're sorted into it again.

### Simple view
- The category name is a 44px button with a chevron that opens the row's details (one at a time; `?cat=` opens it too; Esc closes and returns focus): "Bills in {Month}" (name, amount — the paid amount once paid — and the status in words with a glyph: "✓ Paid Sep 3", "○ Due Sep 28", "! Late: expected Sep 20", "Skipped this month"), a Total, "+ Add a bill" (the calendar's Add dialog with the category chosen; Undo deletes the item), "Includes $X left from {previous month}", and Target, Move money (from this category), Rename, Remove (asks first; Undo takes the removal back with `restore: true`, so the envelope and a negative plan are exactly as they were; later months' rows the removal deleted don't come back) and "See on calendar →" (`#/recurring?d=` the next unpaid bill from today, else the calendar).
- The collapsed row note is the bills summary ("2 bills · next due Sep 28", "Paid Sep 1", "2 bills · all paid", "Late: expected Sep 20") before the carried-over note. Left reads "paid in full" when every counted bill is paid and nothing is left.
- When `bills.total` > `assigned`: "Your bills here add up to $X this month." [Plan $X] [Always plan for my bills] (the second sets the `bills` target and plans the total; one Undo puts back both, the earlier target with `restore: true`). "+ Add a bill" isn't offered on Emergency savings, and its dialog doesn't list it.
- "{Month} is here" / "Use {previous}'s plans": a category with a `bills` target starts at assigned + needed (this month's bills).
- TargetModal (both views) has a third choice, "Cover my bills".

## Phase 2 (not built)
Bills per category (`EnvelopeLine.bills`, `bills_available`, `bills_outside`), the `bills` target kind, income occurrences (the late or short paycheck's name and date), storing Plaid's detailed category and auto-mapping it to Groceries, Eating out and Gas.

---

# Private updates (D5): the updater in the app

Signed `.ftupdate` files that the person who set up FinTrack emails; FinTrack finds them in the user's Downloads folder or they pick one in Settings. Core (file format v2, verification, store, Downloads scanner): `backend/app/updates/README.md` and the `package.py` docstring. Design and codes came from the updates design notes (not in the repo; their v1 zip format is replaced by v2). Launcher contract (install-root `state.json`, exit 75, snapshots, `update_result.json`): `packaging/launcher/launcher_core.py` docstring. App side: `backend/app/updates/service.py` (`Updater`, one per app), `backend/app/updates/install.py` (`InstallJob`, state.json handshake), `backend/app/routers/update.py`.

## Mode (decided once at startup; `updates/mode.py`)
`git` (the code folder has `.git`: the owner's checkout or a worktree) > `not_installed` (no `FINTRACK_INSTALL_ROOT`, no install-root `state.json`, or the code isn't under `<root>/versions/<v>/`) > `dev` (`DEV`) > `enabled`. In every mode but `enabled` the updater never looks up Downloads, never reads or creates `data/updates/`, and every POST below answers 409 `{detail, code:"disabled"}`; status still answers (mode, version, contact).

## Routes (all need the session; errors `{detail, code}`; POSTs need `X-FinTrack`; JSON bodies with unknown fields → 422, except `/file`)
- `GET /api/update/status` (never activity) → UpdateStatus `{mode, support_contact (string|null), current: {version, display_version, installed_at (install-root state.json), last_update_at}, offer: UpdateOffer|null, banner: {show, remind_after}, install: UpdateProgress|null, last_result: UpdateResult|null, rollback: {available, to_version}}`. `banner.show` needs an offer with `can_install`, no "Not now" for it until tomorrow, and no install running. `install` is the running job, or a failed one until its result is acknowledged.
- `POST /api/update/scan` `{}` or no body → UpdateStatus after a Downloads scan (server throttle 30 s: a repeat within 30 s gets the cached answer; single flight). The 5-minute automation loop also scans while unlocked; never while locked or at startup.
- `POST /api/update/file`: multipart/form-data with exactly one `file` part (the browser's file name is reduced to a leaf name). The only multipart path besides `/api/restore` (`security.MULTIPART_PATHS`, exact match). Streamed into `data/updates/.incoming-<hex>/` with a 150 MB cap (413 `FT-UPD-BIG`, also from `Content-Length` before reading), verified, a valid copy kept as `data/updates/inbox/<id>.ftupdate`. → UpdateCheck: `{result:"ready", offer}` (it becomes the offer, source `picked`) | `{result:"rejected", reason, code FT-UPD-SIG|NOTUPD|DMG, file_name}` | `{result:"older"|"same", code FT-UPD-OLD|SAME, file_name, file_version, current_version}` | `{result:"failed", code, file_name, file_version, current_version}` (this exact file failed or was rolled back before; never offered again). One check at a time (429 `busy`); 409 `busy` while installing (checked again, under the lock that an install start also takes, before the file can become the offer); the session is checked again after the upload (401 if it ended meanwhile, nothing offered); 400 `bad_upload` for any other field, a repeated field, no file part, or broken framing. `FINTRACK_DOWNLOADS_DIR` (testing only: real process environment, never a settings file; a packaged launcher drops the user's `FINTRACK_*` variables and sets it only with `--test-downloads-dir <folder>`) replaces the Downloads folder the scan looks in.
- `POST /api/update/dismiss` `{id}` → UpdateStatus with the banner hidden until tomorrow (local date). 404 `stale_offer` when `id` isn't the current offer. A different (newer) offer drops earlier "Not now" dates.
- `POST /api/update/install` `{id}` → 202 UpdateProgress `{id, to_version, step:"backup", state:"running", code:null, at}`. First, synchronously (under the offer lock: never while a picked file is being checked or a scan runs, which in turn never run while an install starts or runs; while the job runs no other offer can replace this one): 404 `stale_offer` (not the offer, its inbox copy is gone, or it is no longer newer); 409 `busy`; 409 `needs_help` `{help:{reason, code}}` (from the offer, or found now by re-verifying the inbox copy with the full install-root environment: runtime, launcher, disk, Python; the offer then becomes `can_install:false`). If that re-verification rejects the file, or it no longer matches the offer's id/version: 202 with `state:"failed", code:"FT-UPD-02"` and nothing started. Then one `InstallJob` thread:
  1. **backup**: `build_backup` → `data/update-backups/fintrack-before-<current>-<YYYYmmdd-HHMMSS>.ftbackup` (newest 3 kept, by time), then a forced automatic backup to the user's backup folder when one is set (its failure doesn't stop the update). Failure → FT-UPD-01.
  2. **install**: install-root `state.json` must say `current` = this version with no `pending`/`rollback`; a leftover `versions/<new>` that is neither current nor previous is removed; `extract_payload` re-verifies the inbox copy (full `Environment`) and unpacks `versions/<new>` (bytecode compiled); its id and version must equal the offer's (else the folder is removed). Then one atomic write: `current=<new>, previous=<old>, pending={version, from, at, id}, rollback=null` (unknown keys kept). Any failure → FT-UPD-02 with nothing changed. Needs-help found only at this point (e.g. the disk filled) → `state:"failed"` with the help code (FT-UPD-HELP-*); the offer becomes needs-help and is not recorded as failed.
  3. **restart**: `step:"restart", state:"restarting"`, then `request_exit(75)` about 1 s later.
  On FT-UPD-01/02 `last_result` = `{outcome:"failed", from_version, to_version, code, at, backup_kept (false for 01)}` with the same `at` as the failed progress. The offer's id is recorded as failed (never offered again; a newer release is a different file) ONLY when re-verification refused the file itself (FT-UPD-SIG, or FT-UPD-DMG not caused by a read error); never for FT-UPD-01 or a disk/file/state error (FT-UPD-02 then leaves the offer in place, the user can try again). If the private copy no longer matches the offer's id, the copy and the offer are dropped (the next scan copies the Downloads file again), nothing recorded. If the offer changed or its copy vanished during the job: `failed`/FT-UPD-02 progress only, no `last_result`, nothing recorded.
- `GET /api/update/progress` (never activity) → UpdateProgress, or 204 when nothing is running and no failure is waiting to be acknowledged.
- `POST /api/update/result/ack` `{at}` → 204; clears `last_result` (and a failed install's progress) when `at` matches.
- `POST /api/update/rollback` `{password}` (optional "Go back to version X") → 202 `{to_version}`, then exit 75. The password is checked like every re-auth (wrong → 401 with `tries_left`, counted; 429 while locked out). 409 `rollback_unavailable` unless: no `pending`/`rollback`, `previous` is an older version whose `run_packaged.py` exists and whose `release.json` `schema_version` equals this version's schema, and no install is running. Writes `current=<previous>, previous=null`: a plain restart, the launcher restores no data.

## Unlock (changed)
- The right password but `migrations.SchemaTooNew` → 409 `{detail, code:"schema_too_new"}` (was 500), in every mode.
- The right password but the data couldn't be upgraded -- a migration/schema error, NOT a passing one (`database is locked`, `locking protocol`, `database schema has changed`, disk I/O, full disk, file in use, out of memory, also when found in the error's `__cause__`/`__context__` chain: those stay a plain error, the user can try again), nor a VaultError such as `pre_migration_copy_failed` -- while install-root `state.json` has `current` = this version and `pending.version` = this version, no open of the data has succeeded in this process, and THIS open started a schema upgrade (the vault's pre-migration copy ran; data already at the latest schema never goes back, whatever the error): `rollback={to: previous (else pending.from; an older version whose files exist), code:"FT-UPD-05"}` is written atomically, exit 75 is requested (once), and unlock answers 409 `{detail:"update_rolled_back", code:"FT-UPD-05", to_version}`. The launcher restores the pre-update snapshot and starts `to`. Without a pending update the error stays a 500.
- The first successful open (unlock, setup, recover, restore, password change) while `current` and `pending.version` = this version finishes the update: FIRST `pending=null` (atomic write, retried on a sharing violation; an in-process flag set at every successful open blocks any rollback even if this write fails), then best effort, each on its own: history `{id, from_version, to_version, at}` (once), `last_result = {outcome:"installed", from_version, to_version, code:null, at, backup_kept:true}`, version folders other than current and previous removed (`.partial-*` are the launcher's), `data/pre-update-*` snapshots and the launcher's `data/failed-update-*` folders cut to the newest 2 each, offers not newer than this version dropped.
- Recover with the sheet opens (and upgrades) the data with the sheet's key BEFORE re-encrypting it: `schema_too_new` → 409 and a data-upgrade failure while pending → 409 `FT-UPD-05` `{to_version}` (rollback as above) with `keyfile.json` untouched, so the restored pre-update copy still opens with the same sheet; the new password is set only after a successful open.

## Startup (lifespan, `enabled` only)
`data/updates/update_result.json` from the launcher → `last_result` (then deleted; malformed files are deleted too); a `failed`/`rolled_back` result with an `id` (the launcher copies `pending.id`) and code FT-UPD-03 or FT-UPD-05 records that id as failed (the launcher's FT-UPD-01, its copy before the update failed, does not). After a `rolled_back` result the `data/failed-update-*` folders are cut to the newest 2. Offers not newer than this version are dropped; interrupted update-backup writes are removed. `cleanup_staging` removes `data/updates/.incoming-*` and `data/.update-*` as before. No scan at startup.

## Security
Signature and payload hash are verified before anything is parsed or unpacked, on the scan copy and again at install; the version must be newer (no downgrade or replay); failed ids are never offered again. Responses carry leaf file names only, never folder paths; logs carry codes and exception types only. The self-exit watcher counts a running install as busy. Automatic rollback only while `pending` names this version, only when the failing open started a migration, and never after an open of it succeeded; the manual rollback needs the password and the same schema version.

# Settings redesign (D7, Release 3.7)

Design handoff: the settings design notes (not in the repo; their README.md is the source of truth for look, sizes and copy). Six tabs: Categories, Paychecks, Reminders, Banks, Safety and backups, Text size and updates, plus a "Needs you" card above the tabs. No schema change: LATEST stays 7; new values live in `app_settings`.

## Owner decisions (2026-09-28)
- **No email** (unchanged, see Recurring): the Reminders tab has no email address, email switch or test email. Bill reminder rows say "Reminder N days before" (in-app).
- **Rules from Settings always apply to past purchases** that weren't set by hand (as every rule does). No "Also change past purchases" checkbox. Add toast: "Purchases from X will go to Y." + " Past ones were changed too." when `matches > 0`. Delete toast: "Rule deleted. Purchases it sorted go back to automatic." with Undo (re-created at its old position).
- **Budget's suggested income = this month's paychecks** when there is at least one active income item (the sum of `calendar.income_occurrences` in the month, moves and skips applied); otherwise the 3-month average as before. `income.suggested_source: "paychecks"|"average"` is added to `GET /api/budgets` and `GET /api/budgets/setup` income.
- **Automatic backups:** "Turn on" uses a suggested folder (OneDrive\Iron Owl backups if OneDrive exists, else Documents\Iron Owl backups; a "FinTrack backups" folder there from before 2.0.0 is suggested instead while the new one doesn't exist), created when missing. "Change folder…" opens the native Windows folder picker run by the server.
- Other defaults (owner may overrule): the category board shows **spending** categories only (groups whose categories are all income/transfer/fixed are left off, like Budget); non-spending categories sit in a collapsed "Income, transfers and loan payments" section (click → edit window: rename, color, hide; no group, no delete for built-ins). Deleting a category a rule uses also deletes those rules (Undo restores both). Undo uses server snapshots. "Save a backup now" keeps its password step. Auto-lock is stored in the vault and overrides the env value; choices 5/15/30/60, no "off". Existing features without a place in the design move to a new page `/settings/banks` (Plaid keys, connections with add/sync/remove, automatic sync hours) and a "More backup options" disclosure (backup file list, previous vaults). Calendar display settings move to the Recurring page. Text size scales text only (px spacing stays). The sidebar Settings dot counts every Needs-you item. Toasts keep the app's 12 s.
- Recovery chip: `active` → "Ready" (green); `none` → "Not made yet" (amber); `unconfirmed` → "Not finished" (amber, button "Finish the sheet…"); `stale` → "Out of date" (amber, "Make a new sheet…"). All four except `active` are Needs-you items.

## Routes (session + `X-FinTrack` on non-GET; StrictModel3 bodies: unknown fields, NaN and Infinity → 422; money in dollars)
- `GET /api/settings` → `{auto_lock_minutes, auto_lock_choices: [5,15,30,60], auto_lock_source: "vault"|"env", password_changed_at: ISO-Z|null, paycheck_notify: bool}`. `PATCH /api/settings` `{auto_lock_minutes?: 5|15|30|60, paycheck_notify?: bool}` → same shape; any other value or null → 422. The idle timeout changes at once and is re-read at every unlock; `GET /api/auth/status.auto_lock_minutes` = the stored value while unlocked, the env value while locked. `password_changed_at` is written by setup, change-password and recover. With `paycheck_notify` false, `GET /api/budgets` returns `income.event: null` and the `income` alert uses its plain wording.
- `GET /api/paychecks` → `{items: [{id, name, amount, cadence, next_date|null, account: {id, name, mask}|null, per_month}], monthly_total, this_month_total, income_mode: "expected"|"off", notify}`. Items = `spending.income_items`. `per_month` factors: weekly 52/12, biweekly 26/12, semimonthly 2, monthly 1, quarterly 1/3, yearly 1/12, once 0 (rounded to cents). `this_month_total` = what Budget suggests (the paychecks due this month). Writes use `/api/recurring` (create needs `next_date`; delete + Undo via `GET /api/recurring/{id}/snapshot` → `DELETE` → `POST /api/recurring/restore`).
- `POST /api/categories` also accepts `group_id: int|null` (422 "unknown group").
- `GET /api/categories/{id}/snapshot` → CategorySnapshot `{category: {id, name, hue, kind, custom, hidden, position, group_id, target|null}, hand_set: [{transaction_id, source}], splits: [split ids], budgets: [{month, assigned, removed, restart}], bills: [recurring ids], rules: [full Rule incl. position], flags: {savings_category, auto_map_key|null, excluded_plan}}`. `DELETE /api/categories/{id}` now also deletes rules that use it (was 409). `POST /api/categories/restore` (body = CategorySnapshot) → 201 TxnCategory: validates everything first, restores what still exists (missing ids skipped), re-creates the rules at their positions, then `rules.apply_all`. 409 when the id or name was taken meanwhile; at most 50,000 ids per list.
- `POST /api/category-groups/restore` `{id, name, position, category_ids}` → 201 CategoryGroup[] (ordered): reuses the id only when free and not above the highest group id in use (otherwise SQLite picks a new one), inserts at `position`, re-groups only listed categories that still exist and are ungrouped. 409 duplicate name; 422 over MAX_GROUPS.
- `POST /api/rules` also takes `position?: int` (insert there, shift the rest; out of range → last). `first: true` = position 0.
- `GET /api/backup/auto/suggested-folder` → `{dir, exists}`. `PUT /api/backup/auto` also takes `create?: bool = false` (make a missing folder, then the usual checks). "Turn on" = PUT (create) then `POST /api/backup/auto/run`. `POST /api/backup/auto/pick-folder` `{}` → `{dir|null}` (null = cancelled; 409 `busy` when a picker is open; 501 `unavailable` off Windows). The picker runs on a worker thread via COM `IFileOpenDialog` with FOS_PICKFOLDERS, owned by the foreground window where possible; it never runs while locked.
- `POST /api/auth/change-password` response adds `backup: {ok: bool}|null`: an automatic backup is forced right after the change when a folder is set.

## Frontend
- `pages/Settings.tsx` becomes a shell: title, Needs-you card, `role="tablist"` tabs with amber dots, `?tab=cat|pay|rem|banks|safe|app` plus the pref `fintrack.ui.settingsTab`. Old deep links: `focus=recovery-sheet(&make=1)` → safe (+ open the dialog), `focus=updates` → app, `focus=bank-connection` → `/settings/banks`.
- Tabs in `pages/settings/{CategoriesTab, AutoSortCard, PaychecksTab, RemindersTab, BanksTab, SafetyTab, AppTab}.tsx`; `pages/SettingsBanks.tsx` for `/settings/banks`. The low-balance card is shared with Recurring's SideCards.
- Text size: `lib/textSize.ts`, pref `fintrack.ui.textSize` = normal|large|larger → root font-size 100% / 112% / 125%, applied in main.tsx before the first render (the lock screen scales too); CSS `font-size: …px` rules become rem.
- Dev mocks cover every new route.

## D7 update 2 (2026-09-28): Rules and Alerts tabs
The settings design handoff was replaced: seven tabs `cat | rules | pay | alerts | banks | safe | app` (Categories, Rules, Paychecks, Alerts, Banks, Safety and backups, Text size and updates). The Rules & alerts page leaves the sidebar; `/rules` redirects to `/settings?tab=rules`. The "Automatic sorting" card in Categories is gone (its footnote points to the Rules tab). The Reminders tab is gone (its parts are in Alerts).
- **Still no email** (owner decision stands): no Email checkboxes, no email address, no test email. The Alerts tab keeps the in-app parts and the read-only bill reminders list ("Reminder N days before", link "Change on the Bills calendar →").
- **Rules tab:** full table (grip + ArrowUp/ArrowDown, When, Then, Used = `matches`, On switch = `enabled`, Edit), drag reorder via `POST /api/rules/reorder` with Undo. Rule window: "is exactly"/"contains" (`op`), optional "Only when the amount is over" (`amount_op: "gt"`, `amount`), visible categories, always-visible preview. **No "Also change past purchases" checkbox** (rules always apply to past automatic purchases). New rules go to the top (`first: true`). Delete with Undo re-creates at its old `position`. "Store" = `field: "any"` for rules made here; rules with other fields/actions (name-only, transfer) are listed with their existing wording and edited in the same window only for the fields it has (the lead accepts a simple "Store/Bank description" label for them).
- `POST /api/rules/preview` (additive) takes `{...RuleInput, rule_id?: int}` and returns `{matches, would_change, sample: [{id, date, name, merchant, amount, from: {id, name, hue}|null, to: {id, name, hue}}]}`: `matches` = transactions this rule fits (splits excluded; other rules ignored, as before); `would_change` = those whose category would actually change if the rule were saved (new → at the top; edit (`rule_id`) → at its position), never counting hand-set (`user`/`user_bulk`) rows; `sample` = up to 20 of the would-change rows, newest first. Copy: "Fits N past purchases · M would change"; zero matches → the design's empty text.
- **Alerts tab rows** (map onto `alert_settings`; `GET /api/alerts/settings`, `PUT /api/alerts/settings/{key}`):
  - Low balance ($ value) → `low` + `low_ahead` together (one switch, one amount = `low.value`).
  - Large purchase ($ value) → `big`.
  - Category over plan → `budget`, which now fires only when spending is **over** the plan (never at exactly 100%).
  - Bill due soon → `reminder` + `due` together (one switch). No number in this row: "Remind me before a bill is due. You pick which bills, and how many days before, on the Bills calendar." (per-bill `reminder_days` stays).
  - New repeating charge → `newrec`.
  - Amount changes → `price` (Release 3.19, "Price changes on hand-set amounts"; `PUT /api/alerts/settings` also takes `price?: {enabled}`).
  - `price` and `conn` no longer create alert events (bank problems still show on Home and in Settings › Banks); their settings rows stay for compatibility. The `income` alert keeps working, governed by `paycheck_notify`.
  - Home hides alert events whose alert is off. The backend may add a combined route if it makes the two-key rows atomic: `PUT /api/alerts/settings` `{low?: {enabled, value}, big?: {...}, budget?: {enabled}, reminder?: {enabled}, newrec?: {enabled}}` → the full list.
- **Text size:** scale the root font size; type **and component spacing** (control heights, paddings, gaps) in rem so layouts reflow. Borders, radii and the page grid may stay px.

### Backend notes (D7, as built)
- **Suggested income** (`services/spending.load_income`): with at least one active income item on the budget accounts (`income_items`), `suggested` = the sum of this month's income occurrences (effective date in the month, calendar moves applied) at each item's amount, `skipped` ones left out, not rounded; `null` when that sum is $0 (e.g. only a quarterly paycheck, not due this month), still with `suggested_source: "paychecks"`. Otherwise the 3-month average (`"average"`).
- **`income_items` leaves out transfers** (`spending.transfer_items`), so the Paychecks tab, Budget's suggestion, "Next paycheck" and "Paycheck was short" never count money moved between accounts: a recurring deposit is a transfer when most of its 12 newest deposits (same merchant key; same account, or any visible account for an item with no account) are `is_transfer` or in a transfer-kind category (e.g. Transfer in). An item whose budget category is an income one, or with no deposits seen yet (added by hand), always counts. A client paying by Zelle every month that Plaid files under Transfer in is therefore not a paycheck until its deposits (or the item) are put in Income.
- `GET /api/paychecks` `next_date`: the item's next `upcoming` occurrence from today (moves/skips applied), else `next_date` when not past, else null. `this_month_total` = the same paycheck sum (0 with no items).
- Auto-lock: `Vault.auto_lock_minutes` = the saved choice while open, else the env value; every lock drops it; the after-unlock hook re-reads it (`services/prefs.load_auto_lock`). A stored value outside 5/15/30/60 is ignored (env value, source `env`).
- Change password: `backup` is `null` when no folder is set, else `{ok}` from one automatic backup run at once (pruned to `keep` like any automatic run); a failed backup never fails the change.
- `PUT /api/backup/auto` `create: true` (strict bool): before anything is made, the path must be absolute and local (lexical, drive type, links of the existing part), outside data/, with at most 3 missing levels of plain folder names (no reserved characters, device names or trailing dot/space); folders made are removed again if making the rest fails; then the usual `validate_dir`. The existing part is walked top-down from the drive root (`link_target`, then `lexists`, per component; stops at the first missing one), so nothing beneath a link is touched before the link is read and found local. `create: true` with `dir: null` → 422 (never read as "turn off"); the UI falls back to the folder picker (then the typed-path box when the picker is unavailable) when the suggested folder is null, and only says backups are on when the response has a `dir`.
- `GET /api/backup/auto/suggested-folder`: the OneDrive folder comes from the OneDrive environment variables, Documents from `SHGetKnownFolderPath`; a base that isn't a local existing folder is skipped (never stat-ed if it looks like a network path); `{dir: null, exists: false}` when neither is usable.
- `POST /api/backup/auto/pick-folder` (body `{}` or none): `{dir}`; errors are `{detail, code}`: 409 `busy`, 501 `unavailable`, 500 `failed` (the dialog couldn't open; logged with the HRESULT or exception type only). No database session is held while the window is open; the session is checked again when it closes (401 if the vault locked meanwhile). Options: FOS_PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST | NOCHANGEDIR | DONTADDTORECENT.
- Category snapshot/restore: only custom ids (`c_<n>`, never reused) that the custom-category counter has already issued can be restored (422 `unknown category id` otherwise). Restore puts back: purchases set by hand that aren't hand-set to something else since; split parts still in Other; budget rows; bills with no category since; rules at their positions (their old id when still free and not above the highest rule id in use; otherwise a new one); the savings category / automatic-sorting row / excluded-plan settings when nothing else took their place. The group comes back only if it still exists.
- `POST /api/category-groups/restore`: the other groups are renumbered 0..n-1 around the restored one.
- `POST /api/rules/preview`: 422 `unknown category` for a category rule whose category doesn't exist (as create).
- Change password: the forced backup can never fail the response once the password changed (any error → `backup: {ok: false}`, logged by type only).
- Every lock (including shutdown) closes an open folder picker (WM_CLOSE to the worker thread's `#32770` window); uvicorn runs with `timeout_graceful_shutdown` 5 s.
- Rules preview: a rule that is off, or a category rule with no category yet, previews as not saved (new: `would_change: 0`; an edit: the rule removed). `would_change` also counts a change of the transfer flag (transfer rules); `from` is null for a transaction with no category.
- Alerts: `PUT /api/alerts/settings` `{low?, big?, budget?, reminder?, newrec?}` (each `{enabled?, value?}`, same checks as the per-key route, all checked before anything is saved) → the full list; `low.enabled` also sets `low_ahead`, `reminder.enabled` also sets `due`; values apply to the row's own key. `GET /api/alerts/events`, `GET /api/summary.unread_alerts` and Home's price cards leave out events of alerts that are off. `budget` ignores its percent value. `price` still makes detected bills follow a new charge's amount once per charge, recorded as a scrubbed cleared tombstone (nothing is shown); price events made before this release still show on Home until 30 days old. `conn` makes nothing.

# Goals (D8) and Investments (D9) redesign (Release 3.8)

Design handoff: the goals-investments design notes (not in the repo; their README.md is the source of truth for look, sizes and copy). Build **Goals layout B** and **Investments layout B** (tiles + a shared "What it holds" card). `Sidebar.dc.html` there is out of date; ignore it. No schema change: LATEST stays 7; new values live in `app_settings`. Toasts keep the app's 12 s `UndoToast`. The design is dark-only; the app keeps light and dark themes.

## Owner decisions (2026-09-28)
- Investments: layout **B**. **No "You put in" / "It has grown by" / dashed "What you put in" line**: Iron Owl has no contribution data. Show "since last month" and "since Iron Owl started tracking ({Mon YYYY})", each noting it "includes any money added".
- Old goals: savings goals show as "Not in your Budget yet" until the user saves them once in Edit (that links them). **Debt goals leave the Goals page** (the Loans debt planner covers payoff); their rows stay in the database, untouched. Nothing is deleted.
- Defaults (owner may overrule): goals need the Budget set up first (the page shows "Set up your Budget first →"); "Already saved" comes from Not planned yet, then from the Emergency fund if that isn't enough (the window says so), and is read-only in Edit afterwards; the Budget's "Emergency savings" category becomes the Emergency fund goal (renamed to the goal's name); Investments keeps the old detailed holdings table behind a "Detailed view" switch; the real fund name shows small and muted under each plain type.

## Goals: model (no migration)
- A goal is linked to one spending category: `app_settings.goal_links` = `{"<goal_id>": {"category": "<id>", "seed_month": "YYYY-MM"|null, "prior"?: {"name", "hidden", "savings", "added"?}}}` (`prior`: the category existed before the goal and was taken over; see Delete; `added` = `{month, taken, rows: [{month, before, after}]}`: that category's budgets rows (T−1 and T, each `[limit_cents, removed, restart]` or null for no row) before and right after the add, and the cents it took from Not planned yet, for the add's Undo); links to a missing category are dropped (the goal shows "Not in your Budget yet"). The `goals` row is reused: `kind` gains `emergency` (VARCHAR, no CHECK); `target_cents`, `monthly_cents` keep their meaning; `target_date` = the first of the due month; `account_id` is only the "Keep the money in" label; `current_cents` is unused.
- Category: the Emergency fund takes over `budget_savings_category` when set (renamed to the goal's name; its old name, hidden flag and savings role are kept as the link's `prior`) and sets it when created. Other goals get a new custom spending category (`budget_setup.add_category`) in the group named "Other and saving" (ignoring case); else the savings category's group; else a new "Other and saving" group.
- **saved** = max(0, available(c,T) − max(0, assigned(c,T))) (the envelope, not counting this month's plan; month end needs no job). **reached** = saved ≥ target.
- **This month's plan**: assigned(c,T) = min(monthly, max(0, target − saved)), written with `spending.save_budget_cents` on create (and when a goal joins or rejoins the Budget); a monthly or target change (and "Set aside $N") changes assigned(c,T) by the **difference** between the new and the old rule's plan (same saved), so money moved in or out on the Budget page ("Use …", "Put it in …", Move money) stays, and a name-only or account-only edit never touches the Budget; a lower plan only takes back this month's own plan and never more than the envelope still has: assigned(c,T) goes down at most to `lowest_plan` = assigned − min(max(0, assigned), max(0, available)), so the envelope never ends below $0 and Saved never drops (the over-assign rule applies; refusal → 422 code `not_enough_unplanned`, detail "Only $X isn't planned yet this month. Use a smaller amount, or take money from another category on the Budget page.", `not_planned: X`).
- **New months**: `budget_month.suggested` for a goal category = the same rule (0 once reached); the existing "{Month} is here / Use last month's plans" flow plans them. No automatic rows.
- **Already saved** (new goals only) = a `Budget(T−1, cat, X)` row (like setup's seed). It comes out of Not planned yet; if short, the emergency category's assigned(T) is lowered by the rest. T−1 lines get `EnvelopeLine.seeded: true` ("Money you already had").
- **Delete / "I spent it"** (both reasons): delete the goal and its link. **A category the goal made** leaves T's Budget (`removed`, releasing its leftover to Not planned yet; an overspent one lowers Not planned yet by the overspending, which `save_budget_cents(release_overspent=True)` allows), is hidden if it has no transactions, and is recorded in `app_settings.retired_goal_categories` (list of ids). **A category the goal took over** (`prior` set) is handed back as it was: old name (unless another category took it since), hidden flag (only if it isn't in the Budget), `budget_savings_category` restored; it stays in this month's Budget with its money. Undo via the `restore: true` path of `save_budget_cents`, only reversing a removal the delete itself did.
- `BudgetMonth.savings_category`: the emergency goal's category → `budget_savings_category` (not yet taken over) → the first unfinished goal's category → null. The Budget's "Put it in / Use {Emergency fund}" buttons use that category's name and are hidden when it's null.
- Emergency suggestion = 3 × this month's bill total (calendar occurrences, as setup's Bills row, `budget_setup._month_bills`), rounded up to $100; no bills → 3 × (spending + fixed) as today.
- **The page's Undo right after adding a goal** (review 2, 2026-09-28) is its own path, `DELETE ?reason=undo_add`. A goal that made its own category is deleted as above (its money, Already saved included, goes back to Not planned yet), with no Undo record. A goal that took over a category (`prior.added` set) puts that category's rows back **exactly** as before the add (`spending.put_back_rows`: the T−1 row's value or no row, this month's plan), so Not planned yet and Emergency savings' money are what they were, then hands the category back (name, hidden flag, savings role). Only in the month of the add, while both rows are still as the add left them, and when the put-back doesn't leave the envelope overspent by money spent since the add (available(T) after the put-back ≥ min(0, available(T) before it)); otherwise 409 "This can't be undone any more: {name} changed on the Budget page since. You can still delete the goal in its Edit window." (or "…: a new month has started. You can still delete…") and nothing changes. The page's later Delete / "I spent it" keep the hand-back above.
- **Settings › Categories** (review 2): `DELETE /api/categories/{id}` of a category that holds a goal (a live link, including Emergency savings a goal took over) → 409 "“{name}” holds your goal. Delete the goal on the Goals page."; `PATCH {hidden: true}` on one → 409 "“{name}” holds your goal, so it stays on the Budget page. To remove it, delete the goal on the Goals page." (a hidden category can't be picked for purchases and its purchases would show as needing a category); `PATCH {kind}` that changes one from `spending` to another kind (review 3) → 409 "“{name}” holds your goal, so it stays a spending category. To change it, delete the goal on the Goals page." and nothing changes (the goal would lose its Budget category). The Categories tab shows "Holds a goal · Edit on the Goals page →" (`/goals?edit={id}`) in that category's window instead of Delete and the Show/Hide switch.
- Also: Home's budget numbers leave out every goal category (not only the savings one); the `budget` alert skips goal categories.

## Goals: routes (money in dollars, months `YYYY-MM`; session + `X-FinTrack` on non-GET; unknown fields, NaN, Infinity → 422)
- `GET /api/goals` → `GoalsState` `{month, month_money, not_planned, monthly_total, budget_ready, emergency_suggestion|null, emergency_available|null, goals: [Goal]}` (**replaces the old `Goal[]`**). `emergency_available` = what's already in Emergency savings when no emergency goal exists yet.
- `Goal` = `{id, kind: "emergency"|"save", name, category_id|null, in_budget, target, due_month|null, monthly, planned_this_month, lowest_plan|null (see "This month's plan"; null when not in this month's Budget; the Edit window's preview stops a lower plan there too), saved, account_id|null, account_label|null ("Savings ··2210"), status: "on_track"|"behind"|"reached"|"no_plan", months_to_save|null (due − this month + 1), projected (saved + monthly × months_to_save, capped at target), projected_month|null (emergency: month it reaches the target; null when monthly 0), short_by|null (target − projected when behind), needed_monthly|null (ceil((target − saved)/months_to_save) rounded up to $5)}`. Debt goals are not returned.
- `POST /api/goals` `{kind, name (1–60), target > 0, due_month (save: T+1..T+24; emergency: null), monthly ≥ 0, already_saved ≥ 0, account_id?}` → 201 `GoalsState & {goal_id}`. 409 "Set up your Budget first." while setup is needed; 409 "You already have an emergency fund."; 409 duplicate category name; 422 `not_enough_unplanned` as above.
- `PATCH /api/goals/{id}` `{name?, target?, due_month?, monthly?, account_id?, plan_before?: {month, planned}}` → `GoalsState` (`plan_before` is only sent by the page's Undo of an edit: after the other fields, this month's plan is set back to exactly `planned` instead of moving by the rule's difference; 409 "This can't be undone any more: a new month has started." when `month` isn't this month, 409 when the goal isn't in this month's Budget, 409 "This can't be undone any more: money from {name} was spent since. You can change the monthly amount in its Edit window." when `planned` is below `lowest_plan` (money spent since the edit stays covered, the same floor as a lower plan), 422 `not_enough_unplanned` when Not planned yet can't cover it; `kind` and `already_saved` can't change; a monthly change rewrites this month's plan; PATCHing an unlinked old goal links it).
- `DELETE /api/goals/{id}?reason=undo_add` (the add's Undo, see above) → `{state: GoalsState, kept_in: string|null, released}` (`released`: dollars that went back to Not planned yet).
- `DELETE /api/goals/{id}?reason=deleted|spent` → `{state: GoalsState, undo: {goal: {kind, name, target, due_month, monthly, account_id}, category_id, month, assigned, released, seed_month, removed, token}, kept_in: string|null}` (`released`: what went to Not planned yet, negative for overspending, 0 when nothing left the Budget; `removed`: this delete took the category out of T's Budget; `kept_in`: the handed-back category's name). `POST /api/goals/restore` (body = `undo`, sent back unchanged) → 201 `GoalsState`. The server keeps its own record of each delete in `app_settings.goal_undo` (by `token`, this month only, last 20) and restores **from that record, once** (the record is taken out by one conditional write, `UPDATE … WHERE value = <what was read>`, as the request's first write, so two restores arriving together can't both use it): a missing or used token, or a `category_id` that doesn't match → 409 "This can't be undone any more." (a double-clicked Undo never makes two goals); other body fields are ignored. Also 409 when the month changed, the category is gone, another goal uses it, a second emergency fund, a taken name, or the Budget needs setup (linked goals).
- `GET /api/goals/suggestions` is removed.
- **Backend details (build notes, 2026-09-28):**
  - `GoalsState` also has `emergency_basis: "bills"|"spending"|null` and `emergency_monthly|null` (the monthly figure before × 3); both null once an emergency goal exists. `not_planned` is Ready to assign as-is (can be negative when over-planned).
  - `in_budget` = linked **and** a member of this month's Budget. A goal whose category was removed on the Budget page shows `in_budget: false` with its `category_id`; any PATCH puts it back. Unlinked goals: `saved` 0, `planned_this_month` 0, status from `monthly`.
  - `status`: `reached` (saved ≥ target) → `no_plan` (monthly 0, either kind; never "behind") → `behind` (save goal, projected < target) → `on_track`. `months_to_save` is at least 1 (a past due month leaves this month). Emergency: `projected` = target when monthly > 0, else saved. `projected_month` = T + ceil((target − saved)/monthly) − 1 (this month's plan is month 1), for both kinds; null when reached, monthly 0, nothing left to save (an old goal's target of 0) or past year 9999. `needed_monthly` is sent for every unreached dated goal.
  - POST order: this month's plan must fit in Not planned yet first; "Already saved" then takes what's left, then lowers the emergency category's plan this month (at most min(available, carryover + assigned)); never the goal's own category. The seed refusal is also 422 `not_enough_unplanned`, with `not_planned` = what's left after the plan and `emergency_available`.
  - An emergency goal that takes over Emergency savings starts from what's already in it (`emergency_available` = saved(line), so money moved out this month has already come off); `already_saved` adds to that (the T−1 row is raised). If Emergency savings' assigned(T) is negative (money moved out this month) it is kept as is (the plan starts next month) instead of being raised to the plan.
  - PATCH: a `target` or `monthly` change moves this month's plan by the difference (see "This month's plan"). Linking an old goal needs a due month (save goals: 422 "Pick the month you need the money by.") and a target > 0 (422 "Type how much you want saved."). A goal whose category is no longer a spending category (changed to another kind before Settings refused that; still named like the goal) gets 409 "“X” isn't a spending category any more, so it can't hold this goal. Give the goal another name to put it back in your Budget."; a PATCH with a new name links it to a new category. `already_saved` is accepted only on the PATCH that links an old goal (else 422). `due_month` for save goals is T+1..T+24 when changed (the stored value may be sent back unchanged); emergency: null only.
  - A new goal whose name matches a hidden **custom** spending category that a deleted goal left (`retired_goal_categories`) and no goal uses reuses that category instead of 409 (any other hidden category with that name → 409 "You have a hidden category named “X”. Pick another name for the goal.") (money released when it left the Budget doesn't come back). Exception: if that category left this month's Budget and `already_saved` > 0, 409 "A goal named “X” was deleted this month. Pick another name, or add it without Already saved." (its envelope restarts this month, so a T−1 row couldn't reach it).
  - DELETE's `undo` is POSTed back unchanged (see restore). `reason` (deleted|spent) only changes the page's words. Deleting an emergency goal whose category it made clears `budget_savings_category`; handing a taken-over one back restores it; restoring the goal points it at the goal's category again.
  - `EnvelopeLine.seeded` is also true for the guided setup's own T−1 seed row.
  - Goal bodies use strict numbers ("600" and `true` → 422); names 1–60 after trimming (an old goal's longer name is kept until it is linked; restore accepts up to 200 for unlinked goals).
- Budget (additive): `EnvelopeLine.goal: {id, kind, saved, target, reached, plan}|null` (`plan` = the goal's plan for that month, 0 once reached; the Budget's quick fills — "Use last month's plans", "Use what you spent", "Assign what you did last month" — give a goal category this instead of last month's numbers), `EnvelopeLine.seeded: bool`.

## Investments
- Data honesty: worth = account balance; history = real balance snapshots only (investment accounts are never estimated, so history starts when the user linked or added the account). Plaid security `type`, `subtype`, `is_cash_equivalent` are now kept in `app_settings.security_info` = `{security_id: {type, subtype, cash}}`, written by `sync._apply_holdings` (replaced each sync).
- Classification (`services/investments.py`, first match wins; Plaid's type decides where it can, names only sort funds and holdings of unknown type): **cash** (type cash, `is_cash_equivalent`, ticker `CUR:*`) → **company_stock** (type equity, whatever the name says, unless it is a fund: Plaid subtype `etf` / `mutual fund` / `fund of funds` / `real estate investment trust` / `hedge fund` / `private equity fund` (the exact strings of plaid-python's `Security.subtype`), or a name with index / fund / a word ending in "fund" / ETF / ETN, or "trust" together with stock, bond or commodity words, e.g. "SPDR S&P 500 ETF Trust", "Invesco QQQ Trust"; such funds go on down the list) → **cash** (name money market/cash/sweep/settlement) → **other** (type cryptocurrency/derivative/loan, or name gold/silver/bitcoin/ether/ethereum/crypto/commodity) → **target_date** (name target/retirement/lifecycle/freedom/lifepath and `20\d\d`; `year` from the name) → **company_stock** (no type known yet, i.e. before the first sync after the upgrade, a ticker, and no fund words index/fund (also "Contrafund")/ETF/ETN/trust/portfolio/admiral/institutional/500/total stock/QQQ: "Microsoft Corp") → **bond** (type fixed income; name bond/treasury/fixed income/aggregate/TIPS/municipal/stable value) → **target_date** with `year` null (a target/retirement/… name without a year, with a fund type or fund words) → **intl_stock** (international/intl/ex-US/developed/emerging/foreign/Europe/Pacific/EuroPacific) → **other** (world/global/ACWI/all-world: US and international stocks together, e.g. "Vanguard Total World Stock ETF", "Vanguard Global Equity Fund"; checked after international, so "All-World ex-US" stays international) → **us_stock** (the name says US stocks: 500/S&P/total stock/total market/stock market/extended market/broad market/large-, mid-, small-, mega-, all-cap/Russell/Nasdaq(-100)/QQQ/Dow Jones/US stock/US equity/domestic/growth/dividend/value/blue chip/equity income/institutional index) → **us_stock** (a fund by type, subtype or name that says stock/stocks/equity/equities, e.g. "Fidelity Stock Selector All Cap Fund") → **other** (a fund that says nothing specific, e.g. real estate or balanced funds). Plus **not_broken_down** = worth − holdings total (or the whole account when the bank shares no holdings: "Your bank doesn't say what this holds"). Plain words and colors live in the frontend.
- `GET /api/investments` → `{as_of, last_month, total: {worth, change|null, change_pct|null, change_missing: [account names], tracked_since|null, change_since_tracking|null}, accounts: [{id, name, institution_name, category: "retirement"|"hsa"|"investment", source: "plaid"|"manual", worth, change|null, change_pct|null, tracked_since|null, updated_at, connection: {item_id, status: "ok"|"login_required"|"error"|"pending", kind, last_synced_at}|null, holdings_known, holdings_differ, mix: [Mix]}], mix: [Mix], holdings_differ, history: {total: [Point], by_account: {"<id>": [Point]}}}`. Accounts: non-hidden, those three categories, largest first. `change` = vs the last snapshot on or before the end of last month (null when none: "New this month"); accounts without one are listed in `change_missing` and left out of the total change.
- `Mix` = `{kind: "us_stock"|"intl_stock"|"bond"|"target_date"|"cash"|"company_stock"|"other"|"not_broken_down", year|null, value, pct, names: [holding names]}`, largest first. Top-level `mix` covers all accounts (pct of the total).
- `Point` = `{month, worth, carried, added: [account names]}`: up to 61 months, oldest first, from the first month with a snapshot; month value = the month's last snapshot (carried forward, `carried: true`, when none); the last point = today's worth; `added` (total history only) = accounts that start that month.
- **Backend details (build notes, 2026-09-28):** `as_of` = the newest `updated_at` of the listed accounts (ISO UTC, null without accounts); `last_month` and `tracked_since` are `YYYY-MM`; `change_pct` is a percent with one decimal (5.2, not 0.052). Each account also has `change_since_tracking` (worth − its first real snapshot); `total.change_since_tracking` is the sum of those (an account that joins later isn't counted as growth) and `total.tracked_since` the earliest account's month. `not_broken_down` needs at least $1 (smaller gaps are rounding) and has `names: []`; an account's `holdings_differ` = its holdings add up to at least $1 more than its balance (a stale balance: the mix shows what it holds, pct of the mix total, and the page notes "Your bank's numbers don't quite match; showing what it holds."), top-level = any account; `pct` is of the account's (or all accounts') mix total. `security_info` keeps entries only for securities still held; other banks' entries survive a sync. Estimated snapshots are ignored.
- Frontend: small SVG growth chart (offer only ranges with data; under 2 months of history show a calm note instead of the chart; mark months where an account joins). Gains in green; losses in normal text with "−", never red. A bank needing sign-in: red banner + "Sign in again" via `useBankReauth` ("connected again" toast). `GET /api/holdings` and `GET /api/accounts/{id}/history` stay for the Detailed view.

# Home v2 (Dashboard v2 handoff, Release 3.9)

Design handoff: the dashboard-v2 design notes (not in the repo; their README.md is the source of truth for look, sizes and copy; the page keeps the name **Home**). The build plan and owner decisions below were mapped on 2026-09-28.

## Home v2: Owner decisions (2026-09-28)
- Page name stays **Home** (keep the app's nav: Budget, Reports, etc.; ignore the handoff Sidebar.dc.html).
- **Not now** lasts until something changes (existing vault dismissals + fingerprint), not session-only.
- **Large purchases** appear in "Things that need you" for 3 days ("Was this you?"), filtered by the `big` switch.
- **IBM Plex Sans** approved: add `@fontsource/ibm-plex-sans` (OFL 1.1), import only latin-500/600/700 in main.tsx; token `--font-num: "IBM Plex Sans", var(--font)` with tabular numbers. Fonts must not inline as data: URIs (CSP has no font-src data:). Backend: `mimetypes.add_type("font/woff2", ".woff2")` and font/woff at startup.
- Recommended defaults adopted: "bill due" only for bills with a reminder on; "Checking after" follows the Bills calendar's everyday-spending setting (and the note says so); per-day = left ÷ (days from today through payday); show 3 needs then "Show N more"; hide the Investments tile when there are no investment accounts; never read localStorage for financial data.

## Home v2: Baseline
- No old Dashboard page in code; replace pages/Home.tsx, pages/home/*, GET /api/home, services/home.py.
- Home needs today: bank_signin, bank_error, finish_setup, backup_failed, needs_category, price_up (+ client low_balance, recovery_sheet). Settings update 2: price/conn stop, budget alert only when over, paired switches low+low_ahead and reminder+due, helpers alerts.disabled_keys() and PAIRED.
- Goals/Investments: exclude goal categories from Home budget numbers; need light helpers goals.state(..., bm=) and investments.totals() (no 61-month history on the dashboard).
- Dark tokens already match the design; keep light theme too. rem for type and spacing (D7 text size).

## Home v2: Parts
- Header: greeting, date, "· Updated {relative}" from cash.updated_at; Update from banks → sync quiet success, toast "Updated from your banks just now.", header "Updated just now". Sync failure toast text should not name the page oddly.
- Things that need you: server-built, server-filtered by Settings switches. Sort by tone (act, warn, info) then kind order: bank_signin, bank_error, finish_setup, low_balance, over_plan, backup_failed, bill_due, card_due, new_recurring, needs_category, big_purchase, recovery_sheet. Live kinds computed at load (not from alert_events):
  - low_balance (moved to server) key `low_balance:{acct}`, fp dip date, switch `low`, calendar first dip within 35 days.
  - over_plan key `over_plan:{cat}`, fp YYYY-MM, >3 → `over_plan:many`; switch `budget`; spent > carryover + assigned; skip savings and goal categories; data {category_id, name, planned, spent, over, month}; copy "Eating out is $18.75 over plan".
  - bill_due key `bill_due:{recurring_id}`, fp base_date; switch `reminder`; occurrences with reminder_days > 0, today in [d−N, d]; data {recurring_id, name, date, amount, kind, account_name, days}.
  - card_due key `card_due:{acct}`, fp due date; switch `due`; liability next_payment_due within due.value days.
  - new_recurring key `newrec:{item}`, >3 → `newrec:many`; switch `newrec`; RecurringItem status suggested, created in last 30 days.
  - big_purchase key `big:{event}`; switch `big`; big events from last 3 days.
  - Existing kinds unchanged; drop price_up. recovery_sheet stays client-side from /api/recovery.
  - UI: 18px radius rows, amber for act/warn, card colors for info, light 44px primary link + Not now; actions wrap inside their row (screenshot 01 overlaps at mid widths — don't copy). "N things need you". Empty + no error → green strip "You’re all caught up. Nothing needs you right now."
- Left to spend (highlight card): add budget.next_paycheck {date, name, amount}. "N days left" = days_left + 1 (count today). "About $X a day until your paycheck on {date}"; no paycheck → "about $X a day for N more days"; paycheck today → "Your paycheck comes today. About $X a day for the rest of {Month}." (the last day of the month: "Your paycheck comes today, the last day of {Month}."); a per-day amount that rounds under $1 reads "Less than $1 a day", never "About $0 a day". Plan bar role=progressbar; "$X spent" / "of $Y planned · N%". Pills → /spending, /recurring. Keep "Set up your budget" empty state; over state "Over by $X", full bar, never red.
- Money you have: 34px total, "In checking and savings", inset list pinned bottom, rows ≥56px (name ··mask, bank, balance), link /accounts, max 4 rows + "N more →"; keep stale/manual wording.
- Small tiles: Came in this month (months current came_in; next paycheck; → /transactions?view=in&start=YYYY-MM-01); Saved toward goals (Σ saved, "N goals in progress", "Set up your Budget first" when not ready); Investments worth ("Up $X since last month" green, "Down $X" plain with −, "New this month" when change null; hidden with no accounts).
- Coming up: rows in [today, today+7] (counted, card, plan, upcoming + carried late/pending first), sorted counted_on/date, then the row's own date, in before out, then name; max 8 + `more`. `after` = running checking balance consistent with calendar days[].balance (opening = days[d−1].balance − daily when include_daily); null for card rows ("Not from checking"). "Below your warning" shows only on a day's last counted row (`day_end`), whose `after` is that day's balance, the same day-end balances the note, `first_below`, the Bills calendar and the low-balance alert use (a row mid-day can dip for a moment, e.g. a bill listed before that day's paycheck, without the day ending low). Note cases: warning on & no dip "Checking stays above your $T warning amount all week. Lowest point: $X on {date}."; dip "Checking could drop to $X on {date}, below your $T warning amount."; warning off "Lowest point: $X on {date}."; already below "Checking is below your $T warning amount now."; daily on → add "Counts about $D a day of everyday spending." No forecast account → "Choose your checking account". Keep migrateDailyPref() before first load. Remove BillsCard and lib/homeForecast.ts.
- Left over each month: custom HTML/CSS bars (not Recharts); 5 full months + current ("So far", dashed, tooltip "Not finished yet"); every bar tabIndex=0 + aria-label, tooltip on hover and focus "{Month} · Came in $X · Went out $Y · $Z left over"/"$Z more went out"; dynamic scale top = nice(max(maxPos, 3×maxNeg, $100)), zero at 75%; no-data months "—"; computed note ("Money was left over in 4 of the last 5 months. In July, $105 more went out than came in."), counting only months with data; no full month → "After your first full month…".
- Goals mini: up to 3 unfinished goals, "$saved of $target", 10px accent bar, "All goals →"; none → "Start your first goal →".
- Dropped: net worth block/chart (stays on Accounts/Reports), status line (backup failures stay as backup_failed need; version in Settings), BillsCard, legacy forecast, price_up. Keep Welcome card for a brand-new vault (fix its link to /settings/banks) and the recovery-sheet nudge.

## Home v2: API: GET /api/dashboard (replaces GET /api/home; dismissals stay at /api/home/dismissals/{key})
Session + unlocked; per-section isolation (failed section → null, listed in `errors`); compute budget_month and build_calendar(today, today+35) once and share.
```json
{ "today": "2026-09-28",
  "setup": {"...": "as HomeData.setup"}, "banks": [{"...": "as HomeData.banks"}],
  "cash": {"total": 0, "updated_at": "ISO", "accounts": [{"id":0,"name":"","mask":"","institution_name":"","balance":0,"source":"plaid","item_id":0,"subtype":"checking"}]},
  "budget": {"month":"YYYY-MM","has_budget":true,"days_left":2,"planned":0,"spent":0,"left":0,
             "next_paycheck": {"date":"YYYY-MM-DD","name":"","amount":0} },
  "coming_up": {"account": {"id":0,"name":"","mask":"","institution_name":""}, "from":"","to":"",
                "balance":0,"threshold":500,"warning_on":true,"daily_spend":null,
                "rows":[{"key":"r12:2026-09-29","recurring_id":12,"date":"","counted_on":"","name":"","kind":"in|out|card|plan",
                         "account_name":null,"amount":0,"status":"upcoming","still_expected":false,"after":0,"day_end":true}],
                "more":0, "low": {"date":"","balance":0}, "first_below": null},
  "months": [{"month":"YYYY-MM","came_in":0,"went_out":0,"left_over":0,"complete":true,"has_data":true}],
  "goals": {"budget_ready":true,"total_saved":0,"in_progress":0,"top":[{"id":0,"name":"","saved":0,"target":0,"status":"on_track"}]},
  "investments": {"has_accounts":true,"worth":0,"change":0,"change_missing":[]},
  "needs": [{"key":"over_plan:c_7","kind":"over_plan","tone":"warn","dismissible":true,"fingerprint":"2026-09","data":{}}],
  "dismissed": {"key":"fingerprint"}, "errors": [] }
```
months: last 5 full + current from reports.totals_by_month; went_out = spending + fixed; has_data = on/after first transaction month. Removed vs /api/home: version, forecast, low_alert_enabled, backup, net_worth.
Refresh: one GET on mount via useApi; invalidate after sync / sign-in again / backup / failed Not now; when the window comes back (visible again or focused) refetch if the data is >5 min old, and always when the local date differs from `today` (the PC slept through midnight); also at local midnight; no polling.

### Home v2: backend notes (as built, 2026-09-28)
- `services/dashboard.py` + `routers/dashboard.py`. **`GET /api/home` and `services/home.py` are removed** (404); `routers/home.py` keeps only `PUT`/`DELETE /api/home/dismissals/{key}` (same key/fingerprint rules and vault store `home_dismissed`). `POST /api/backup/auto/run` is unchanged. The client filters with `dismissed` + fingerprint (needs_category keeps its `t{id}` rule). The server drops needs only for the two roll-up rules under "Needs data" (`over_plan:many`, `newrec:many`).
- `errors` names: `cash | budget | coming_up | months | goals | investments | needs` (in that order); `setup`, `banks`, `dismissed` are not isolated. `budget_month` and `build_calendar(today, today, today+35)` are each built once; when one fails, every section and need kind that reads it is null / listed (`budget` + `goals` + `needs`; `coming_up` + `needs`); the goals card then tries its own month. A switched-off kind is never built. Logs carry the exception type only.
- `cash.accounts` order: the forecast (checking) account first, then checking, savings (Plaid subtype), then other cash accounts (manual ones without a type included), each group by name (case-insensitive); the page shows them in this order.
- `budget` also keeps `is_current`; `left` = `planned − spent` exactly (negative when over); `next_paycheck` = `GET /api/budgets` `income.next` (`{date, name, amount}` | null). Goal categories and the Emergency savings category are left out (as before).
- `coming_up`: with no forecast account `{account: null, balance: null, rows: [], more: 0, low: null, first_below: null}` plus `from`/`to`/`threshold`/`warning_on`/`daily_spend`. `warning_on` = the `low` switch; `daily_spend` = the everyday amount when that setting is on, else null. Rows: late/pending ones still counted (`still_expected: true`) first, then by `counted_on` (else `date`), then by `date` (so an unpaid bill due today, counted tomorrow, comes before tomorrow's paycheck and its `after` doesn't include that pay), in → out → card → plan, then name; `amount` signed; `status` upcoming|late|pending; `account_name` = the item's account (a card for card rows; checking for items with no account; null for plans); plans excluded from the forecast aren't listed. `after`: per `counted_on` day, opening = `days[d−1].balance − daily` (daily only when on), plus each counted row's amount in order, so a day's last row equals `days[d].balance`; card rows null. `day_end: true` marks each counted day's last row (worked out before the 8-row cut, so a day cut short has none); the page flags "Below your warning" only on `day_end` rows under `threshold` with `warning_on`. `low` = the lowest day in [today, to] (earliest on ties); `first_below` = the first day in [today, to] under `threshold` (null when `warning_on` is false; `date == today` = already below).
- `months`: `spending.totals_by_month` (the Reports definitions: visible bank/credit/HSA/other accounts, split-aware, transfers out); `went_out = spending + fixed`; `has_data` = on or after the month of the first transaction on those accounts (hidden accounts don't count). `partial: true` marks that first month when its first transaction is after the 1st (history started mid-month): the page shows it muted ("Started mid-month") and leaves it out of the "left over in N of the last M months" note (the note counts months with `complete && has_data && !partial`). `complete` still only means "the month has ended".
- `goals` = `goals.summary(session, today, bm)`: per-goal numbers are `goal_out`'s (so they match `GET /api/goals`); `total_saved` sums every goal (reached ones too), `in_progress` counts unfinished ones, `top` = the first 3 unfinished, oldest first, status `on_track|behind|no_plan`. `budget_ready` = not `setup_needed`.
- `investments` = `investments.totals(session, today)`: the same `worth`/`change`/`change_missing` as `GET /api/investments` `total` (no holdings, no history); `has_accounts` false → worth 0, change null.
- **Needs data** (amounts positive unless noted; all dismissible except bank_signin):
  - `low_balance` (warn) `{account_id, account_name, date, balance, threshold, cause: name|null}`; needs threshold > 0 and the first dip in [today, today+35].
  - `over_plan` (warn) `{category_id, name, planned (carryover + assigned), spent, over, month}`: only envelopes with something planned (planned > 0, as the `budget` alert); spending with no plan shows on the Budget page under "Spending without a plan". More than 3 → key `over_plan:many`, fp `"YYYY-MM:{count}:{digest}"` (digest: 8 hex of the over categories and their amounts, so any change gives a new fp), data `{count, month, total_over, names: [the 3 furthest over]}`. "Not now" on it (with this month's current fp) also stores what was over (`app_settings.home_dismissed_over` = `{month, fp, over: {category: cents over}}`); while that dismissal stands, nothing over_plan is returned that month (no single needs, no smaller roll-up) until something gets worse: a category not in the snapshot goes over, or one is over by more than its snapshot amount; then the normal needs come back. A "Not now" with an out-of-date fp stores no snapshot (plain fingerprint rule).
  - `bill_due` (info) `{recurring_id, name, date, amount, kind: out|card, account_name, days}`: money going out only (bills and card charges; never paychecks or other deposits), calendar occurrences with `reminder_days` > 0 and status upcoming (not paid, skipped, or moved out of the window), today in [date − N, date]; the nearest per item; at most 10. Only items on the Bills calendar (the forecast account, no account, or a card), like the `reminder` alert.
  - `card_due` (info) `{account_id, name, mask, institution_name, due_date, days, minimum_payment|null, account_category: credit|loan}`: visible card and loan accounts due within the `due` value's days (0–31; default 3) that owe something (balance > 0) and whose minimum payment isn't $0 (unknown counts), at most 10.
  - `new_recurring` (info) key `newrec:{item}`, fp item id, `{recurring_id, name, amount, kind: "out", cadence, account_name|null}`: charges only (money out, "New repeating charge"), never deposits; on a visible account or none (hidden accounts' items are left out); more than 3 → key `newrec:many`, fp `r{newest id}`, `{count, names: [first 3]}`. While `newrec:many` is dismissed (fp `r{N}`), items with id ≤ N are left out (singly and in the count); only newer ones show, as single needs or a new roll-up.
  - `big_purchase` (info) key `big:{event}`, fp event id, `{event_id, transaction_id, name, amount, date, account_name, account_mask}`: `big` events created in the last 3 days, not cleared, whose transaction still exists on a visible account, newest first, at most 10. When a pending purchase posts, the sync's carry-over (`sync._carry_over_pending` → `_move_big_event`) rewrites the event's `dedupe_key` from `big:{pending id}` to `big:{posted id}` (same event: id, time, "Not now" kept; no-op when there is none or the posted row already has one), so the need stays and shows the posted amount. An event whose transaction is gone (removed by the bank or deleted) is left out rather than shown from its stored text, which would describe a purchase that no longer exists.
  - Switches (`alert_settings.enabled`; a missing row counts as on): low_balance `low`, over_plan `budget`, bill_due `reminder`, card_due `due`, new_recurring `newrec`, big_purchase `big` (the Alerts tab's paired rows set `low`+`low_ahead` and `reminder`+`due` together).
- Startup registers `font/woff2` (`.woff2`) and `font/woff` (`.woff`) with `mimetypes` so the bundled Plex font is served with the right type under `nosniff`.

## Home v2: Build order
Backend (services/dashboard.py + routers/dashboard.py from home.py helpers, live needs + Settings filtering, goals/investments light helpers, woff2 MIME, tests adapting test_home.py) → api.ts types + mock → frontend (font, Home.tsx + home/*: CaughtUp, StatTiles, ComingUpTable, LeftOverChart, GoalsMini; rework NeedsYou, LeftToSpendCard, MoneyCard, HomeHeader; delete NetWorthBlock, StatusLine, BillsCard, lib/homeForecast.ts) → SPEC "Dashboard v2" section.

# Credit card payments are transfers (Release 3.9.1)

Paying a credit card counted twice: the card's purchases are spending, and the payment from checking (Plaid primary `LOAN_PAYMENTS`, a `fixed` kind) counted again as a loan payment. The card side was wrong too: a payment the card files under `TRANSFER_IN` counted as money that came in on the Budget, and one filed under `LOAN_PAYMENTS` offset real loan payments (in the next month, when the two sides straddle a month end). From this release a card payment is **money moved between the user's own accounts**, exactly like a transfer they mark themselves. No schema change (`LATEST` stays 7); `services/card_payments.py`.

**Mechanism.** Rule runs (`rules.apply_all`, after every sync, rule or category change) and syncs mark a card payment `is_transfer = true` with the new `transfer_source = 'auto'`. Everything that already leaves transfers out follows with no second definition: the "Spending definitions" (spending, fixed, income, refunds), Reports ("Including fixed bills" and the year), Home's `months` (`went_out`, `left_over`), the Budget (envelopes, "Spending without a plan", its fixed list, `received`), "Needs a category" (a transfer never needs one), the Transactions money in/out, the `Transfer` badge and the CSV `transfer` column, the `big` alert and Home's `big_purchase` need (which now also skips an event whose transaction became a transfer after it fired), and the everyday-spending amount.

**Which transactions** (`card_payments.Context.is_payment`):
- **The card side** (a credit card account): Plaid's detailed code `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` (stored since schema v7), either sign, or money **into** the card whose Plaid primary is `LOAN_PAYMENTS` or `TRANSFER_IN` (the payment as the card sees it, or a balance transfer): never income, spending or a refund. Refunds keep their spending category and still net out.
- **The bank side** (a bank/other account) is marked **only when it pairs** with a card-side payment (`card_payments._pairs`). There is no name heuristic and no "the user has a card" shortcut: paying a card FinTrack doesn't see (not linked, hidden, linked later than the bank so its older months aren't there, a store card, a second payment of the same amount) is the only record of that spending, so it keeps counting. A false negative is preferred over hiding real spending.
  - **Bank-side candidates**, on a visible bank/other account: the detailed code `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` (either sign; another detailed code such as mortgage, car or student loan never is); and **older rows** (no detailed code, synced before v7): money out with primary `LOAN_PAYMENTS` whose name or merchant has no loan word (mortgage, mtg, loan, lease, student, HELOC, auto loan/auto fin, car payment).
  - **Card-side matches**, on a visible credit card, pending included: money in with the detailed code or a primary `LOAN_PAYMENTS`/`TRANSFER_IN`. A returned payment (money back into checking) pairs with money out of the card that has the detailed code.
  - **A pair**: exactly the same amount the other way, dated within 5 days either way. Each row pairs at most once. All possible pairs are taken nearest dates first, then the lower bank-side id, then the lower card-side id (the same result whatever order rows are read in). Of two equal payments with one card-side row, only the nearer one is marked.
- Hiding or unhiding an account, changing its type, and removing a bank connection re-run the rules, so the marks follow.
- **Syncs.** A new bank-side row has no id while the sync writes it, so the sync marks only the card side; the rule run in `sync.after_sync` then pairs it, before recurring detection and alerts, so a payment whose two sides arrive in the same sync never raises "Large transaction". When the card side arrives in a later sync, the bank payment counts until then (it can raise "Large transaction" / "Was this you?"); the rule run after that sync marks it, and Home's `big_purchase` need for it goes away (it skips events whose transaction is now a transfer).

**Precedence** (`rules.resolve`): the user's own choice (`transfer_source = 'user'`: "Not a transfer" / "Mark as transfer") > a transfer rule (`'rule'`) > a card payment (`'auto'`) > not a transfer. A category rule sets the category and leaves the mark alone. **Hand-set categories and splits win**: a transaction whose category the user set by hand (one, several at once, "change all from this merchant", Undo to a hand-set state) or that they split is never marked, and setting one clears the mark (`rules.release_hand_set`; also in the sync's pending→posted carry-over). Back to automatic (or removing the splits) marks it again. The mark is recomputed on every rule run, so it follows the evidence (a card hidden, a rule added). Updated rule from Release 2 "Applying rules": with no matching transfer rule, `is_transfer` is cleared when `transfer_source` is `rule` or `auto` and the transaction is no longer a card payment; `user` is never touched.

**Existing vaults.** A one-time pass (`card_payments.backfill_once`: one rule run) on the first unlock after the update (`routers/auth.evaluate_after_unlock`, also after a restore), flagged `app_settings.card_payment_backfill_done = '1'`; a failure is logged (exception type only) and retried at the next unlock, counting failures in a row in `app_settings.card_payment_backfill_failures`. After 3 failures in a row it stops (flag `'gave_up'`), so a pass that can't succeed doesn't run on every unlock; a success clears the count. After that, rule runs (after every sync) keep the marks current.

**Recurring and the calendar.** A card payment is still a bill the user has to pay: recurring detection's input (`recurring._history`) keeps money out marked `auto` (other transfers stay out), so card payments are still found and refreshed as bills, stay on the Bills calendar, are matched as paid (matching already counted transfers), and the forecast still subtracts them from checking (Coming up included). A card payment gives a bill **no budget category** (`category_from_rows` skips transfers: at detection, when a suggestion is confirmed, for a manual item's guess), so it doesn't show among a budget category's bills: the card's purchases are the spending. Bills that already have a category keep it. Money into a card is no longer detection input.

**Visible changes.** Reports' fixed bills and Home's "went out" drop by the card payments that pair with a payment on one of the user's visible cards (their "left over" rises by the same); payments to a card FinTrack doesn't see, or from months before the card's history starts, keep counting as before; the Budget's loan-payments line no longer lists the paired ones; a card payment the card files as "Transfer in" no longer counts as money that came in; card payments show the `Transfer` badge and stop asking for a category; a big card payment no longer shows as "Was this you?". The payment's category (Loan payments) is unchanged.

# Accounts, Reports tabs and debt plan (Release 3.10): Accounts part

Pair 1 (Accounts page and sidebar). No migration (LATEST stays 7): new data lives in `app_settings`
(`account_facts`, `account_undo`, `plaid_items_linked`). Service: `services/accounts.py` (the router
stays thin); the bank's own facts: `services/account_facts.py`.

## Owner decisions (2026-09-29)
- Bank-connected accounts can only be **hidden** on Accounts (PATCH `hidden`, Undo = PATCH back),
  with a quiet pointer to Settings › Banks. No route removes a Plaid Item or excludes an account
  from Accounts. `DELETE /api/plaid/items/{id}` (Settings › Banks) calls Plaid item/remove (best
  effort) and deletes the Item with all its accounts, transactions, holdings and snapshots; it
  can't be undone and the connection stays used (Plaid Trial: 10 Items, ever).
- Manual accounts can be removed, with a real Undo.
- `accounts.name` is the nickname (a sync sets it only when it creates the row); the bank's own name
  is in `account_facts`.

## Routes (session + `X-FinTrack` on non-GET; StrictModel3 bodies: unknown fields, NaN, Infinity → 422; dollars)
- `GET /api/accounts`: every account (hidden ones too) with, in addition to the old fields:
  `bank_name` (Plaid's `name`, else `official_name`; null for manual accounts and for a linked one
  until its first sync after this release), `connection` (`{item_id, kind: bank|investment|loan,
  status: ok|login_required|error|pending, last_synced_at}` or null for manual), `balance_date`
  (newest non-estimated snapshot; a manual account with no snapshot at all uses the local day it
  was added), `balance_age_days`, `stale` (manual and ≥ 7 days; ≥ 30 days for category `other`,
  a house or car whose value changes slowly) and `credit_limit` (the bank's
  `balances.limit`, linked credit cards only). A fixed number of queries (no query per account).
  POST and PATCH answer with the same shape.
- `POST /api/accounts` (201): `{name, kind?, category?, current_balance, institution_name?, mask?,
  interest_rate?, minimum_payment?, next_payment_due?, notes?}`. `kind` is one of
  checking→bank/checking, savings→bank/savings, credit_card→credit/"credit card",
  student→loan/student, auto→loan/auto, other_loan→loan/none, investment→investment/none,
  other→other/none ("Something else you own (like a house or car)") (category / `plaid_subtype`,
  so the automatic loan group works). `category` alone still works (older form); both must agree;
  one is required. `mask` is `^[0-9]{4}$`. Loans and cards store the **positive amount owed**
  whichever sign was typed (so `card_due` and net worth work for them); everything else keeps its
  sign (an overdrawn checking account is negative).
- `PATCH /api/accounts/{id}` (older route, same fields): after the category is set, a manual
  loan or card is normalized to the positive amount owed whenever the request sets
  `current_balance` or `category` (`services/accounts.normalize_debt`: the balance and every
  negative snapshot of that account flip to positive; the typed balance's snapshot is written after
  that). Linked accounts keep what the bank says.
- `PUT /api/accounts/{id}/balance {balance}` (manual only; 400 for linked, 404 unknown) →
  `{account, undo: {token}}`. Debts: amount owed, positive. Writes today's snapshot.
- `POST /api/accounts/balance/undo {token}` → Account. Puts back the old balance and today's
  snapshot exactly (deleted if the save created it; its old amount and `estimated` flag if not).
  409 when the token is unknown, used, from another day, of the other kind, the balance was
  changed since (undo newer saves first), or the account isn't the one the token was for (the
  record keeps the account's `created_at` to the microsecond; an account that reused a removed
  one's id never matches, nor does a record without it).
- `DELETE /api/accounts/{id}` (manual only; 400 for linked) → 200 `{undo: {token, name}}`. Also
  clears `forecast_account_id` when it named this account (a reused id must not inherit it), its
  `account_facts` entry and today's balance Undo records for this account id; the budget-account
  choice is pruned as before.
- `POST /api/accounts/restore {token}` → 201 Account, or 409. Puts back the row (the same id if
  still free, else a new one; `created_at` to the microsecond), every snapshot, goals and repeating
  bills whose `account_id` the delete cleared (only those still unlinked, and only the very rows
  the delete saw: the record keeps `[id, created_at, name]` per link and a row is relinked only when
  its `created_at` matches, or, for a row without one, its name; a newer goal that reused an id is
  never linked), its budget-account exception (exclude / include / legacy list),
  `forecast_account_id` (only if none was chosen since) and its `account_facts` entry.
- `GET /api/plaid/status` adds `items_linked` (lifetime count of connections created at Plaid),
  `items_limit` (10) and `items_now` (the connections there are now).
- `PUT /api/plaid/items-linked {count}` (session + `X-FinTrack`; StrictModel3, `count` a strict
  whole number 0-10) → `{items_linked, items_limit, items_now}`. The owner's correction from the
  Plaid dashboard; 422 (plain words, "Type a whole number from N to 10.") below the connections
  there are now or above 10. Stored in `app_settings.plaid_items_linked`.

## Undo records (`app_settings.account_undo`)
`{token: {kind: balance|delete, day, ...}}`, the server's own record (the request only names it):
today's only, the last 20. A token is `secrets.token_urlsafe(16)`, used up by one conditional write
(the stored value must still be the one read), so two Undos at once can't both use it; a refused
Undo rolls back and keeps its token. A balance token can't restore an account and the other way round.

## Bank's facts (`app_settings.account_facts`)
`{"<account id>": {"bank_name", "limit_cents"}}`. Every sync (`sync._apply` → `account_facts.record_sync`)
replaces the entries of the accounts it touched and prunes entries of accounts that no longer
exist; an account the bank left out this time keeps its entry. Only linked accounts show them.
A `limit` that isn't finite or is over 1e13 dollars is ignored before it is multiplied. The step
runs in a savepoint (`sync._record_facts`, after the account upserts are flushed): any error there
is logged (exception type only), its writes are undone and the sync carries on.

## Plaid duplicate check (slot safety)
`routers/plaid.py _duplicates_existing_item` compares Plaid's `official_name or name` (+ mask +
subtype) with the local `official_name`, else `account_facts.bank_name`, else `accounts.name`, so
renaming an account can't let a second (billed, slot-using) Item in. An account renamed before this
release with no official name is only protected after its next sync.

## Connections used (`app_settings.plaid_items_linked`)
Counted in `POST /api/plaid/exchange` whenever Plaid gave a new Item (also a rejected duplicate,
which Plaid created and FinTrack removed); re-exchanging a known Item doesn't count. Never stored →
starts at the number of Items there are now; reading never writes; removing an Item doesn't give it
back. A lower bound: Items created in Link but never exchanged, and Items removed before Release
3.10, can't be seen, so Settings › Banks shows "Bank connections used: N of 10" with a "Change…"
button (a small window: "If you connected banks before, the Plaid dashboard shows the real number.
Type it here.", a whole number from the connections there are now up to 10; `PUT
/api/plaid/items-linked`). Every place that would start Plaid Link for a new bank (the Add account
window, Settings › Banks link buttons and Bank connection's link button, Home's welcome card)
checks the count first; when all 10 are used it shows "You've used all 10 bank connections. You
can still add accounts yourself." instead of opening Plaid Link. Signing in again to a bank
(update mode) is never blocked.

## Debts saved negative before Release 3.10 (one-time fix)
`services/accounts.safe_fix_debt_signs`, run after every unlock (next to the recurring category
backfill) until `app_settings.debt_sign_fix_done` is set: every manual loan or card with a negative
balance, and every negative snapshot of a manual loan or card, becomes positive (the amount owed),
in one commit. A failure is logged (type only) and counted in `debt_sign_fix_failures`; after 3 in a
row it is marked done ("gave up") so it doesn't run on every unlock.

## Home need `update_balance:{account id}`
Tone warn, dismissible, fingerprint = `balance_date` ("none" if unknown), at most 10, stalest first,
visible manual accounts only, 7+ days old (30+ for category `other`); `data: {account_id, name, mask, institution_name, days, balance_date,
account_category}`. Kind order: right after `card_due`. No Settings switch (always on).

## Accounts page: money words (review fixes, 2026-09-29)
- Debts show the amount owed as a plain number next to "owed" (a credit balance: its size, "in your
  favor"). Everything else shows its **signed** amount: an overdrawn account's row reads "−$50.00"
  with the word "overdrawn" in amber (never red); its page says "Overdrawn by $50.00" (amber). The
  "Update balance" box starts at the signed value ("-50.00") for non-debts, the amount owed for debts.
- Add account yourself: checking, savings and "Something else you own (like a house or car)" take
  a negative balance ("Overdrawn? Type a minus sign first, like -50.00"); debts and investments
  don't. The reminder wording follows the account: 7 days, 30 for `other`.
- Loan group rename: the box takes up to 40 characters (the server's limit). One request per loan;
  a failed one is retried once, and if it still fails the user is told plainly how many got the new name
  ("Only 2 of 3 loans got the new name. Press Save name to try the others again.").
- Sidebar: the "Updated …" line isn't a live region (it changes every minute). The Settings link's
  label is "Settings, 3 things need you"; the phone Menu button's is "Menu, on Home, 3 things need
  you in Settings".

# Accounts, Reports tabs and debt plan (Release 3.10) — Reports/Debt part

Reports becomes a page with tabs: **Overview · Spending · Paying off debt**. Year in review is
dropped (`GET /api/reports/year` and its service code are removed; a stored tab pref `year` maps
to Overview). Money is dollars in the API, cents inside; months `YYYY-MM`. Every route needs a
session; non-GET routes need `X-FinTrack: 1`; bodies are `StrictModel3` (unknown fields and
NaN/Infinity → 422).

## Spending tab: `GET /api/reports/spending?month=YYYY-MM`

- Accounts and definitions are the Reports ones: every visible bank, credit, HSA and other
  account; split parts count with their own category; pending counts; transfers (the flag, and
  transfer-kind categories) and income are out. A month's **total = spending + fixed** (loan
  payments) = the sum of its category amounts (refunds net out inside each category).
- `months`: the 6 months ending today's month, `{month, total, complete, has_data, partial}`.
  `has_data` = on or after the month of the first transaction on those accounts
  (`spending.first_data_day`, also used by Home's months); `partial` = that first month when it
  started after the 1st.
- `range = {first, last}`: first = max(window start, first data month) (today's month when there
  is no data), last = today's month. `month` defaults to today's month; malformed or outside the
  range → 422 with a plain sentence.
- `average`: the mean of the window's complete, has-data, not-partial months (≤ 5), `{amount,
  months}`, or null.
- `summary = {month, total, complete, partial, previous}`; `previous = {month, total, has_data,
  partial}` for the month before (it may be outside the window), null when that month has no data.
- `categories` (amount > 0, biggest first): `{id, name, hue (the category's own), kind, bill,
  in_budget, amount, previous (null when the month before has no data), share (whole %),
  purchases, purchase_count, more}`. `bill` follows Reports' bill rule as of min(month end, today).
  `in_budget`: a spending category in today's month's Budget. `purchases`: one row per transaction
  (a split's parts in the category summed, `split: true`), newest first, refunds included so the
  rows add up (`amount` = money out, positive; a refund is negative), `pending`; capped at 100
  (`more` = the rest).
- `stores`: the top 5 merchants by net spending, leaving out bill categories (fixed kind
  included) and payments of Recurring items; `count` = transactions that were money out (a split
  is one); `category` = the one the store spent most in. `stores_left_out` = the names of the
  month's bill categories.

## Paying off debt tab

- `POST /api/debt/plan` (existing) adds, all additive:
  - body `lump?: {amount > 0, account_id | null}`: a one-time payment at the start, to
    `account_id` first (it must be one of the plan's debts, else 422), then in plan order. The
    top-level numbers stay the plan **without** it; `lump = {amount, account_id, applied:
    [{account_id, amount}], paid_off: [ids cleared at once], months, payoff_date, total_interest,
    never}` (null without a lump).
  - `debts[]` (plan order) gain `kind` (`card` = credit; `mortgage` = a mortgage or home equity
    subtype, or the "Home loans" group; else `loan`), `apr_known` (false when the account has no
    rate: counted at 0%), `baseline_months` / `baseline_date` (that debt at its own minimum).
  - `interest_this_month` = Σ round(balance × apr / 1200) at today's balances (an estimate);
    `interest_12` = the plan's interest in its first 12 months; `timeline[].interest` = interest
    charged that month.
  - `baseline = {months, payoff_date, total_interest, never, timeline: [{month, total}]}`: every
    debt at its own minimum, no extra, no rollover ("today's payments"; equals
    `compare.minimums_only`).
  - `skipped = [{account_id, name, reason: "no_minimum"}]` when `account_ids` is omitted: visible
    debts with a balance and no minimum payment (the default plan still includes mortgages).
- `GET /api/debt/progress?ids=1,2,3` (comma-separated, ≤ 100; omitted = every visible loan and
  card; a non-debt or unknown id → 422) → `{months: [{month, total, estimated}], since, change,
  missing, cards}`. Months: the last 12 ending today's month, **honest months only**: a month
  counts when every included debt has a balance snapshot (real or backfilled) on or before its
  last day; the value is the newest such snapshot (today's month: today's balances); `estimated`
  when any value is a backfilled estimate or was carried over from an earlier month. `since` =
  the first month shown; `change` = last − first total (negative = went down; null with fewer
  than 2 months); `missing` = names of included debts whose history starts after the window's
  first month. `cards` = every included credit card `{account_id, name, balance, limit,
  used_pct}`; `limit` comes from `app_settings.account_facts["<id>"].limit_cents` (written by
  sync), read defensively: missing or malformed → null (manual cards too).

## The Budget's "Paying off debt" category

**Loans only (owner decision).** A credit card's balance is already subtracted from the Budget's
money, so card payoff is already in the Budget; filing a card payment here would count it
twice. The planner (`/api/debt/plan`, `/api/debt/progress`) still covers cards; only the Budget
line is loans-only: when a plan includes cards, "Add $X a month to my Budget" budgets the extra
for the included loans only.

- `app_settings.debt_budget = {category, linked, extra_cents, strategy, account_ids,
  created_category}`; Undo records in `app_settings.debt_undo` (this month only, at most 20,
  single use via a conditional write).
- `GET /api/debt/budget` → `{budget_ready, linked, category_id, extra, strategy, account_ids,
  loans: [{account_id, name}] (the stored ids that are still loans: what the amount covers),
  month, planned_this_month (null when not in this month's Budget), not_planned}`.
- `PUT /api/debt/budget {extra > 0, strategy: avalanche|snowball, account_ids: [1..100 loans]}`
  → `{state, undo: {token}}`. A credit card id → 422 "Credit card payments are already part of
  your Budget money, so they don't go in this line."; any other non-loan or unknown id → 422
  "Pick the loans this is for." Budget not set up → 409 `{"detail": "Set up your Budget
  first."}`. It uses the category the plan used before, else a spending category named "Paying
  off debt" (unhidden; another kind or a goal's → 409), else makes one in the goal group ("Other
  and saving"). A first add plans max(this month's plan, extra); a change moves this month's plan
  by (new extra − old extra), never lower than what was already spent from it
  (`goals.lowest_plan`). More than Not planned yet → 422 `{detail, code: "not_enough_unplanned",
  not_planned}`. Every write goes through `spending.save_budget_cents`.
- `DELETE /api/debt/budget` → `{state, undo}`: the category leaves this month's Budget (like a
  goal's delete: leftover back to Not planned yet, overspending comes out of it) and is hidden
  when no transaction uses it; `linked` becomes false (the category is remembered). Not linked →
  409.
- `POST /api/debt/budget/undo {token}` → `{state}`: puts the category's Budget rows back exactly
  (`spending.put_back_rows`), the setting, and the category (one the add made is deleted again
  when nothing uses it, and so is an "Other and saving" group the add made, when it is empty
  then); only this month and while the rows are as the change left them, else 409
  ("This can't be undone any more…") and nothing changes.
- `GET /api/budgets`: `EnvelopeLine.debt = {extra}` on the linked category's line, else null;
  `suggested[category] = extra` for a month with no rows yet.
- While linked, the category can't be deleted, hidden or given another kind (409 pointing to
  Reports › Paying off debt). Home's budget card, the `over_plan` need and the `budget` alert
  leave it out, like goal categories.
- **Filing payments (owner decision)**: it is a normal envelope; money leaves it only when the user
  files a **loan** payment under it. A **credit card payment** can never be filed there, by any
  path: a bank-side payment that pairs with a visible card's payment (`card_payments.Context`,
  so it stays a card payment after a category set by hand clears the "auto" mark), FinTrack's
  card payment mark (`transfer_source = "auto"`), any row on a credit card, or Plaid's
  `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`. A single edit, a split or an Undo
  (`POST /api/transactions/restore`) → 422 "This is a credit card payment. Card payments are already part of your Budget
  money, so they don't go in “Paying off debt”." and nothing changes (its transfer mark stays);
  bulk edits skip those rows (listed in `skipped` and in `skipped_card_payments`, the rest
  change); "apply to similar" skips them (`{changed, skipped: N}` when the category is this one);
  a category rule for it never matches a card payment (the next matching rule decides, as if it
  weren't there); restoring a deleted "Paying off debt" category leaves card payments out.
- Loan payments are fixed-kind (and a transfer rule may mark one), so setting a transaction's
  category to it **by hand** clears a transfer mark (`debt_budget.release_filed`; card payments
  are never touched): single edits and split parts clear any mark (the later choice wins, unless
  the same edit marks it a transfer); bulk edits, "apply to similar" and sync clear only a
  transfer rule's mark, never the user's own "Mark as transfer". The payment then counts exactly once
  everywhere, as spending in "Paying off debt" (it moves from Fixed; totals don't change). A
  payment filed while pending stays counted when it posts (sync runs the same release after the
  pending carry-over). Which mark was cleared is kept per transaction in
  `app_settings.debt_released` (at most 500, newest kept), so the Transactions page's Undo of the
  filing puts that exact mark back (the user's or a rule's), once, and only while the row is still as
  the filing left it; setting that transaction's category, splits or transfer mark by hand again
  forgets it. An Undo (`/api/transactions/restore`) or a deleted category's restore that puts
  payments back under "Paying off debt" runs the same release (a rule's mark, never the user's), so
  they count again.

# Budget workspace (Release 3.11, frontend)

The default Simple Budget view is a compact group selector above the selected group's full-width category table. This replaces the simultaneous expanded group cards; Detailed view, setup, income shortfalls, targets, bills, goals, debt links, and spending-without-a-plan remain available. No accounting, API, or schema change. Wiring plan: `docs/BUDGET_WORKSPACE_PLAN.md`.

- Group selectors follow existing position order, show remaining spendable/reserved money separately where applicable, and retain an overspent-category warning even when the group has money left. At wide widths five fit per row; ten groups occupy two rows. A labeled dropdown replaces cramped selectors; narrow category rows retain visible Planned/Spent/Left labels. Groups are data, not fixed names.
- Selected group is a UI preference; a category deep link selects its group and reveals its row. Invalid or rejected inline edits must not be discarded by changing group or month. Valid changes save on Enter/blur; Escape cancels. Existing queued saves and Undo apply.
- Four-cell overview: Money for the month (`month_money`), all Planned (`assigned`), spending-only Spent, and Left in spending plans. Spending-only scope matches Home: exclude `savings_category`, `goal`, and `debt` categories using metadata. Left is their signed available sum, including carryover. A breakdown explains scope, carryover, reserves, and unplanned money; expected income is visibly identified rather than presented as guaranteed cash.
- Give it a plan opens a category-and-additional-amount dialog, previews the new plan, Left, and unplanned amount, and saves through the existing API only when the user acts. Cover remains the current source picker with unplanned money first when available and accurate latest balances.
- Month navigation uses the existing bounds. Past months are read-only; current-only income editing stays current-only. Current/future valid assignments retain existing validation.
- Budget options contains the existing advanced tools. Contextual row details preserve bills, targets, rename/removal and appropriate goal/debt/calendar links. Transaction navigation is explicitly category-filtered when group filtering is unavailable.
- Money shortfalls use amber with words; all actions remain keyboard accessible, text-size aware, and compatible with both themes. No real sample groups or screenshot amounts are seeded into the vault.

# Budget category cards (Release 3.12)

Simple Budget retains the group selector and overview from Release 3.11. The selected group's table becomes compact rectangular category cards. Large Spent and its funding comparison appear on one line; a usage bar separates them from percent used and money left. Category icons and hues provide identity; completed bars remain neutral, and shortfalls use amber with words.

- Clicking a card opens its category panel on the right. Narrow screens stack the panel above the cards. The panel contains Planned this month with explicit Save and Cancel, read-only Spent/Left, and the viewed month's transactions. A funding comparison includes carryover when present; Planned edits only the month's assignment.
- Existing mutation validation, queued saves and Undo apply. Valid drafts save before changing category, group, month, closing, removing a category, or opening a contextual tool. Invalid or failed drafts remain visible and block that transition until corrected, retried or canceled. Past months remain read-only. A negative savings assignment is shown as money used, with adjustments through Move money.
- Bills, targets, rename/remove, Move money, and goal/debt/calendar links remain under Category options. The group's See transactions action chooses a category and opens its Budget panel. Detailed view and all other Budget tools retain their existing behavior.
- `GET /api/budgets/{month}/categories/{category}/transactions?limit=50&offset=0` requires the normal unlocked session and returns `{items, total, next_offset, spent}`. Items contain `{id, date, name, account_name, amount, pending, split}`. Dollars are signed like transactions. Only included Budget accounts and matching month/category ledger contributions count; transfers are excluded. Split contributions for the same parent/category aggregate into one item. Pending purchases count, refunds reduce spending, and the full-month spent total is floored at zero. Pagination does not change that total. Unknown spending categories and invalid paging/month parameters are rejected.
- Transaction loading, empty state, retry and pagination stay inside the panel. Split contributions are labeled Category part; pending purchases and refunds are labeled. Refreshes discard stale category/month/page responses. These changes add no schema or financial write endpoint.

Implementation and verification plan: `docs/BUDGET_CATEGORY_CARDS_PLAN.md`.

# Budget: remove and delete from the Budget page (Release 3.13)

Amends Release 3.12 (remove is no longer under Category options).

- The whole category card is the click target and lights up on hover; buttons inside it (Cover) keep their own action. The card's toggle button stays the keyboard and screen-reader control.
- The category panel ends with an always-visible "Don't need this category?" section with two choices, each with an inline confirm and Undo: **Remove from {Month}'s plan** (the existing remove-from-plan; the category stays) and **Delete category** (same as Settings › Categories: deleted everywhere, its purchases go back to automatic sorting, the rules that use it are deleted; Undo restores its snapshot). Delete is offered only for categories the user made: budget lines carry `custom` (`GET /api/budgets` `categories[]`). Savings, goal and debt categories get neither choice; past months show neither.
- A group's header has **Delete group** (not for "Not in a group"). Its categories stay in the budget without a group (as Settings does) and the page shows them there; Undo puts the group back at its place with the same categories. Confirms close on the first click (no double delete) and "Keep it" or Escape returns focus to the button that opened them.
- No schema change and no new endpoint.

# Reports v2: Overview, Habits, Year over year (Release 3.14)

Design: the reports-v2 design notes (not in the repo; their README.md is the contract for layout, copy, behavior and colors;
layout **A · Summary first**). Amends Release 3.2 and 3.10's Reports part. This section lists only
what the README leaves open or what differs from it; where they disagree, this section wins.

- Tabs: **Overview · Habits · Year over year · Spending · Paying off debt**
  (`?tab=overview|habits|yoy|spending|debt`). Spending and Paying off debt are unchanged.
- Spending rules everywhere on these tabs are the existing Reports ones (`spending_lines`: the same
  accounts as `GET /api/reports/monthly`, pending counts, refunds net out, split parts count with
  their own category, transfers left out). Bills are the existing inferred `bill` flag.
- Pref `reports.hideBills` defaults to **true** when nothing is stored; a stored value is kept.

## Overview
- One month can now be any month of the report (stepper `‹ ›`); the frontend asks
  `GET /api/reports/monthly` for enough months to cover the stepper plus the 3 "usual" months.
  (Release 3.18 replaces the stepper with the month picker, back to the first month with data.)
- `GET /api/reports/monthly` adds `months[].planned`: `{category_id: dollars}`, the month's budget
  plan (`budgets.limit_cents`) for spending and fixed categories with a plan; `{}` before budgets
  exist. A range's plan is the sum of its months' plans.
- **Action buttons** (current month only; Undo restores the previous value with the same save path):
  - *Plan $X for {next month}* / Habits *Set the cap*: `api.budgets.save(nextMonth, {assigned:{[cat]: X}})`.
    A refusal shows the server's message and nothing changes. Undo is exact (this and *Move $X to
    savings*): read each touched row with `GET /api/budgets/{month}/rows/{category}` before the save
    (`{state: {assigned, removed, restart}|null}`, null = no row) and again after it; Undo sends
    `PUT /api/budgets/{month}/rows` `{rows: [{category, state, expected}]}` (StrictModel3, 1-10 rows,
    each category once; `state` null deletes the row; `expected` = the row right after the save).
    All or nothing: when any row isn't `expected` any more (changed since, e.g. in another window)
    it answers 409 `This plan was changed since, so it wasn't undone.`; otherwise `put_back_many`
    (one Ready to Assign check): only those rows change, past months are refused, and it is refused
    (422) when it lowers Ready to Assign and leaves it below zero (raising it is always allowed).
    Returns the month (`BudgetMonth`).
  - *Move $X to savings*: this month, from the category to `BudgetMonth.savings_category`
    (`GET /api/budgets/{month}`), X capped at what the category has left; hidden when there is no
    savings category or nothing is left.
  - *Remind me* (Habits, merchant idea): turns on the existing all-categories **Budget alert**
    (`api.alerts.updateSetting('budget', {enabled:true})`). Toast: `Budget alerts are on. You’ll get a
    note when a category goes over its plan.` + Undo. Already on: the button reads `Reminder is on`,
    disabled. (No per-category or 80% reminder: owner's decision.)

## `GET /api/reports/yoy?mode=year|month` (read-only, unlocked session)
- `year`: Jan 1 → today vs Jan 1 → the same date last year. `month`: the 1st of this month → today vs
  the same days last year. "Same date" on Feb 29 is Feb 28; a range never runs past its month's end.
- Response `{mode, current: Side, previous: Side, first_month: "YYYY-MM"|null}`, Side =
  `{start, end, total, months:[{month, total}], categories:[{id, total}], merchants:[{name, category, total, count}]}`.
  Spending categories only (bills included, fixed kind left out, matching the Overview's spending total);
  merchants grouped by `merchant_key` and named like "Where, exactly"; `category` = the merchant's
  most-spent category id; `count` = `visits`. Months in `year` mode run Jan → this month, the last
  month cut at the same day in both years. No previous-year data: `previous.total` 0 and the UI shows
  the README's EmptyState.

## `GET /api/reports/subscriptions` (read-only, unlocked session)
- Items (owner's final rule): Recurring items that are money out, not dismissed and not one-time,
  **charged to a credit card**: the item's account is a visible credit account, or, when the item has
  no account, more than half of its matched charges are on visible credit cards. Still left out: loan
  payments (fixed-kind categories and the debt category), card bill payments and transfer-kind items.
  Bills paid from checking (rent, phone, insurance) are not listed. Items with no category stay
  (`category` null). `{items:[{id, name, category, cadence, monthly, amount, change: {amount,
  month}|null, full_year, card: {id, name}}], monthly_total}`; `card` = the card (`accounts.name`);
  `monthly` = the amount converted to a month by cadence. Selection and price change live in one
  service function, `reports.subscription_list`, for reuse by the Recurring page.
- `change`: from the item's matched charges in the last 12 months (same merchant, same account rule as
  `_is_recurring`, not limited by amount tolerance): the most recent month whose charge differs from the
  one before by more than $0.50 and more than 1%; `amount` is signed (positive = went up). No history:
  `null` (the UI shows `Same price as last year` only when there were charges all year, else nothing).

## Mocks and tests
Both new routes and `planned` get mocks (`dev/mockA.ts`), with at least 13 months of merchant-level
mock spending so Year over year shows stores. Backend tests for both routes and `planned`; frontend
unit tests for the README's Verification list.
- Owner decisions: Subscriptions are recurring charges on a credit card only (final; replaces the
  earlier "all except loans" and "Regular payments" wording). The Habits section keeps the design's
  title "Subscriptions"; the Recurring page redesign shares the same rules and wording helper.
  Year over year hides Month by month in "{Month} so far" mode.

# Bills and paychecks: Recurring page redesign (Release 3.15)

Design: the recurring-v2 design notes (not in the repo; their README.md is the contract for layout, copy, behavior and colors;
layout **A · Big calendar**; layout C is the model for phones only). Amends Release 3.6's calendar
page. This section lists only what the README leaves open or what differs from it; where they
disagree, this section wins.

## Names (owner's decisions)
- Page title **Bills and paychecks** (was "Cash forecast"). The sidebar label stays **Recurring**.
- Links and mentions elsewhere say **Bills and paychecks**: Home (was "Bills calendar"), Settings tabs
  (Alerts, Paychecks), Budget (was "Recurring calendar"), toasts, the command palette's keywords
  (keep "recurring" and "calendar" as search words), README.md. "Left out of the forecast" becomes
  "Left out of the balance"; the dismiss toast points to "Find a bill or paycheck".

## Behavior changes
- **Card charges have no Mark paid** (Coming up and the Item details window). They still show
  `✓ Paid` once the charge is matched. Owner's decision.
- **Adding:** double-click a day, or its **+** button (single click; keyboard and touch). A single click
  on empty day space no longer opens Add. Double-clicking a chip never opens Add. The header button
  opens Add with tomorrow filled in.
- Quarterly and yearly already exist end to end (no backend change). The Add dialog's "How often"
  shows the README's six choices.
- There is no transfer kind: kinds are in / out (paid from checking) / card. The README's "To savings"
  status and "transfers" wording are not used.
- "Possible repeating charges" keeps today's `candidateWhy` wording (the API has months, not dates).
- Late and low banners are removed except the single amber low line under the strip (README Banners).
- Print is landscape (README Print).

## Numbers
- **Lowest point**: the lowest `days[].balance` from tomorrow to the end of the viewed month (not
  `months[].low`, which includes today). When no day of the viewed month is left (today is its last
  day), it uses the next month through its end, labeled `Lowest point in {Next month}`.
- **Paychecks**: the paycheck series is the largest (per month) Settings › Paychecks item
  (`/api/paychecks`) that is also an income series on this calendar's account; otherwise the largest
  (per month) income series on the calendar. Next paycheck = its next occurrence. A pay period runs
  from one of its occurrences to the day before the next. A period's rows are the `out` occurrences
  from checking (not card charges, not budget plan items), leaving out skipped ones and ones left out
  of the balance; other money in during a period is listed as income in that period. **Left for
  everyday spending** = the paycheck + other money in − those bills (`$X short` when negative). With no
  income series: the Next paycheck card reads `No paycheck yet` / `Add your paycheck to see what each
  one covers.`, and Paycheck to paycheck shows that sentence with an **Add your paycheck** button
  (Add dialog, type Paycheck).
- **Bills left**: upcoming + late `out` occurrences in the viewed month (card charges left out).

## Subscriptions
Same rules as Reports › Habits (Release 3.14): `GET /api/reports/subscriptions` and the
`habitsMath.ts` wording helpers (`subscriptionsLead`, `priceNote`). Each item adds `next_date`
(`YYYY-MM-DD` | null, the item's next occurrence) for the `Next: Oct 8 on your card` line.

## Tests
Frontend unit tests for the README's Verification list (strip numbers, pay periods, the double-click
guard, subscriptions wording shared with Reports). Backend test for `next_date`. Mocks keep working;
add a mock scenario with no income series.

# Theme switch (Release 3.16)
A manual choice of colors: **Match Windows** (default, and for anyone with nothing saved), **Light**,
**Dark**, **Warm night** (dark with warm, low-blue colors, like Windows Night light). Frontend only.
- Where: the sidebar's **Colors** button (bottom of the sidebar; its four choices open in a small panel
  to the right of the sidebar, kept inside the window, so the button never moves), the same rows in the phone Menu sheet, and Settings › App ("Colors, text size and
  updates" tab) card **How Iron Owl looks** (tiles with a color sample; a toast with Undo). All three read
  one saved choice and stay in sync, also across FinTrack windows. Picking shows the new colors at once.
- Saved per browser in localStorage (`fintrack.ui.theme`, `lib/prefs.ts`); Match Windows saves nothing.
- Mechanism: `<html data-theme="light|dark|warm">`; no attribute = Match Windows. Every dark CSS block is
  written twice: inside `@media (prefers-color-scheme: dark)` for `:root:not([data-theme])`, and for
  `[data-theme="dark"]` / `[data-theme="warm"]` (`:where()` keeps specificity). Light is the base, so
  `data-theme="light"` is light even when Windows is dark. Dark blocks use hue variables
  (`--h-neutral`, `--h-accent`, `--h-amber`, `--h-amber-2`) so Warm night shifts every page at once.
- No flash: `public/theme-boot.js` (a same-origin file; the CSP blocks inline scripts) sets the attribute
  and the title-bar color before the first paint; `main.tsx` `initTheme()` keeps it in sync.
- Warm night: warm-gray surfaces (hue 60), a muted copper accent (hue 55), cream text. "Needs you" moves
  to a brighter yellow-gold (hue 88-92) with a strong border (`--amber-edge`) so it stays apart from
  the accent. Body text >= 4.5:1, large text and control/warning borders >= 3:1 (checked in
  `scripts/theme.test.ts`). Money-in stays green and category colors keep their hues; never red for money.
- Switching turns CSS transitions off for one frame (`.theme-switching`), so nothing fades between themes.
- Accent-colored highlights (Home "Left to spend", the debt plan box, toasts, the Habits calendar, the
  Overview summary) follow `--h-accent`. Dimmed things (past calendar days and rows, months outside the
  period, paused rules and alerts) use a quieter text color, not opacity, so words keep >= 4.5:1.
  "Needs you" badges and "Choose category" are 13px in `--warn-text` (>= 4.5:1).
- Charts read color tokens, so all four themes work. Mock: `?theme=light|dark|warm|system` shows a theme
  without saving it.
# Budget plans repeat each month (Release 3.17)

Amends Release 2/2.1 ("`assigned(c, m)` is the row for exactly month m"), Release 3.6 (the
"Use last month's plans" card), 3.8 and 3.10 (goal and debt suggestions) and 3.14 (Reports
`planned`). Owner decisions (2026-09-30).

## The rule
- **A category's plan repeats into later months.** In a month where a member category has no
  `budgets` row of its own, its plan (`assigned`) is the plan of its latest earlier row (the same
  carry-forward as membership). Editing a month's plan writes that month's row; later months
  without their own row then repeat the new amount. Viewing a month saves nothing.
- **Start month:** `app_settings.budget_plans_repeat_from = "YYYY-MM"`, set once, to the month
  this version first runs (first unlock after the update, or setup of a new vault), and never
  moved. Months **before** it keep the old rule (no row = $0), so their envelopes, carryover and
  everything built on them are exactly what they were. Absent = nothing repeats. Note: Not
  planned yet is today's number on every month's view, so it changes on past months' views too
  once today's month repeats plans.
- **What repeats:** the plan part of the latest earlier row (`limit_cents − moved_cents`, below).
  These repeat as **$0**: a negative plan (money moved out of the leftover), setup's "money you
  already had" row (`budget_setup_seed`) and a goal's "Already saved" row (`seed_month`).
- **Removal stops the repeat** (a `removed` row: not a member, nothing repeats); re-adding (a new
  row, or a `restart` row in the same month) repeats the new plan from then on.
- **Goal categories** keep the goal's rule in a month without its own row: min(monthly, target −
  saved), $0 once reached (saved = carryover − spent, as `EnvelopeLine.goal.plan`). The **"Paying
  off debt"** category plans its `extra`. (These were the old card's suggestions for them.)
  These rules use today's goal and debt settings. **Once a month is over it keeps the goal and
  debt plans it showed:** they are written as that month's own rows (exactly the amounts it
  planned, so nothing it shows changes), and `app_settings.budget_rules_frozen_through =
  "YYYY-MM"` records the last month done (a month that already has a row counts as frozen).
  The rows are written, each time in a short transaction of its own that commits right away,
  on unlock, by the automation loop once per new month while unlocked (independent of Plaid),
  before every goal and debt change (create, edit, delete, restore, the extra and its Undo) and
  before every change that can change which category a goal or debt uses (category edit,
  delete and its Undo, group delete and its Undo, the Budget setup). Every Budget, Home or
  Reports read and every save freezes **in memory only** (reads never write or hold the
  database's write lock), so months the app stayed open into, or wasn't opened in, show the
  plans they had with the settings they had: settings only change through those routes.
  Remaining edge: until the freeze is saved (at most until the loop's next check after the
  month ends, or the next unlock), recategorizing an ended month's transactions changes that
  month's goal plan (it depends on what was spent).
  The rules plan only months after `budget_rules_frozen_through`; earlier months without a row
  repeat rows like any category (a goal added later never rewrites them). A frozen row is just
  the plan the month showed: if the category stops being a goal or debt category, later months
  repeat it like any last month's plan. A goal edit in a month without its own row moves that
  month's plan from the plan it had before the edit (the same "change by the difference" rule).
- **More planned than the money:** everything still repeats; Not planned yet goes below zero and
  the Budget shows the existing amber over-planned note (lower a plan). Saves that lower Not
  planned yet below zero are refused as before; saves that raise it are always allowed.

## Later months and Not planned yet (replaces `− Σ_{m > T} assigned(c, m)`)
- Repeated plans in later months set nothing aside now: each month's money pays its plans when
  that month starts.
- A later month's **own row** sets aside now only the part its plan **raises above the highest
  plan so far** for that category (today's plan to start with): raising October's Groceries from
  $500 (repeated) to $600 takes $100 from Not planned yet now; November repeating $600 takes
  nothing more. Removing a category in a later month and re-adding it there up to the highest
  plan so far takes nothing (that month's money pays it); its leftover is released. Lowering a later month frees nothing now, and a save that only lowers amounts is
  never refused.
- Money in a later month's envelope counts as "here now" only for today's leftover plus what was
  set aside now. A move out of a later month (Move money, a negative plan) comes first out of
  that month's own plan (that month's money: frees nothing), then out of the money here now
  (freed, back to Not planned yet), then out of earlier months' repeated plans (frees nothing).
  Example: December's Food leftover $430 = $190 left today + October's and November's repeated
  $120s; moving all $430 out frees $190.
- Moves between categories in the same later month take nothing new; money moved in beyond what
  was moved out that month comes from Not planned yet now.
- Before the start month a later row sets aside its whole amount (the old rule).
- This can change today's Not planned yet when rows for later months were saved before this
  release (they used to set aside their whole amount).

## One-time moves (migration v8)
- `budgets.moved_cents INTEGER NOT NULL DEFAULT 0`: the part of a month's `limit_cents` that came
  from one-time moves. `limit_cents` stays the month's full amount (every existing reader is
  unchanged); the plan that repeats is `limit_cents − moved_cents`. No backfill: rows saved before
  this release (moves included) count in full as plans.
- **Move money** and **Cover it** (and Reports' "Move $X to savings", and a goal's "Already
  saved" taken from the emergency fund) are one-time: after moving $50 from Fun to Groceries in
  October, November repeats Groceries $500 / Fun $500.
- `BudgetSave.moved: {category: dollars}`: that month's one-time moves per category (absolute),
  already included in `assigned`; each key must be in `assigned` too (422 "A one-time move needs
  the category's new amount too."). A category saved **without** a `moved` entry has none: the
  number the user types is that month's plan (the month's moves for it fold in) and is what repeats.
- `EnvelopeLine.moved` (dollars) is the month's one-time part of `assigned`.
- Exact Undo carries it: `GET /api/budgets/{month}/rows/{category}` state has `moved`; `PUT
  .../rows` states accept it (missing = 0); goal and debt Undo records store 4-item row states
  (older 3-item records read as moved 0); the category delete snapshot's `budgets[]` has `moved`.

- Known gap: the Budget page's Undo (`useBudgetMonth.ts`) puts a month back by saving amounts, so
  undoing a change in a month that had no row of its own writes the repeated amount as that
  month's own row (same numbers; later edits to earlier months then don't repeat into it). Being
  replaced by exact row restore in a separate change.

## Removed
- `BudgetMonth.suggested`, the "Use {previous month}'s plans" card and the Detailed view's
  "Assign what you did last month" button: plans now repeat by themselves.

## Reports and review
- `GET /api/reports/monthly` `planned`: spending categories report the month's `assigned` (its
  own row, or from the start month on the repeated plan; one-time moves included), plans of $0 or
  less left out; fixed categories keep their own rows. `/api/spending/review` `history[].assigned`
  is the same per-month total. Before the start month both are exactly the rows, as before.

## Mocks and tests
`dev/mockBudget.ts` / `mockA.ts` repeat plans the same way (start = the mock's current month),
keep moves one-time and accept saves to later months. Backend: `tests/test_budget_repeat.py`
(repeat, start month before/after, edit then the next month, removal and restart, negative and
"already had" rows, goal and debt, over-planned months, later rows saved before this release,
one-time moves and their Undo, Reports `planned`, migration v8 and its rollback). Existing tests
leave the start month unset (conftest), so they keep the old rule unless they pin it.

# Reports: pick any month (Release 3.18)

Amends Release 3.14's Reports part. Both tabs use the shared month picker
(`components/MonthPicker.tsx`: `‹ ›` arrows and a searchable list of months, newest first).

## Year over year
- Toggle: **This year so far** | **One month**. One month shows the month picker; it starts on the
  current month and lists every month from `first_month` to the current month. Month by month stays
  hidden in One month.
- The current month stays "so far": the 1st to today vs the same days last year (as in 3.14).
  A finished month compares the **whole** month with the **whole** same month last year (all of
  March 2026 vs all of March 2025; February is 28 or 29 days on each side, by its own year).
- A month whose last-year month has no spending shows the normal page (rows say "new this year").
  "Not enough history yet" is only for This year so far; One month always keeps its controls. The
  toggle shows whenever any data exists (`first_month` set), even with under a year of history.
- `GET /api/reports/yoy?mode=month&month=YYYY-MM`: `month` is optional (default the current month)
  and ignored in year mode. 422 for a bad format, a year before 1900 or a month after the current
  one (the message never repeats the input). The response adds `month` (`"YYYY-MM"`, null in year
  mode) and `partial` (true when the range is cut at today: year mode and the current month).
- Words for a finished month: lead `In March you spent $X, $Y less than March 2025.` (`more than`,
  or `about the same as March 2025`); stats `All of March 2026` / `All of March 2025`, difference
  `−11.8% compared with March 2025`; the note next to the toggle `compared with all of March 2025.`
  A month from an earlier year names its year: `In March 2025 you spent …`. This year so far and
  the current month keep the 3.14 words.
- Mocks: `dev/mockA.ts` answers `month` the same way. Tests: `tests/test_reports_v2.py` (ranges for
  past months and leap Februaries, 422s, `month`/`partial`), `scripts/reportsHabits.test.ts` (words).

## Overview
- One month: the month picker replaces the `‹ ›` stepper (which reached back 6 months). It starts on
  the current month and lists every month back to `first_month`, but no further than 56 months
  before the current one (the 60-month report limit minus the 3 "usual" months). When the loaded
  months reach that far, months at the start with no money in or out are left out (as before).
- `GET /api/reports/monthly` adds `first_month` (`"YYYY-MM"`|null): the month of the first
  transaction on the spending accounts (`first_data_day`), whatever `months` is.
- Data: the page loads 9 months as before; picking an older month loads enough months for it and
  its 3 usual months, in whole years (12, 24, … up to 60), and never fewer than already loaded. The
  page shows its loading skeleton until they arrive. "Usual" and every number are unchanged for the
  months shown before; an old month's usual is its 3 months before (fewer when history starts later).
- Last 3 months, Last 6 months, Money in and out and the category trend still show the last 6 months.
- Tests: `tests/test_reports_v2.py` (`first_month`), `scripts/reportsOverview.test.ts` (picker range,
  fetch sizes, numbers unchanged with more months loaded).

# Recurring: finding more bills and Maybe on the calendar (Release 3.19)

Owner's decisions (settled): bills whose amount changes are found when their dates are very regular,
and show the **last** charge's amount; a monthly bill is suggested after 2 charges; slightly different
bank names and a bill moving to another card/account stay one series; suggested ("Maybe") bills show on
the calendar with Yes / No; Maybe bills never count in balances, totals or reminders. Plaid's own
recurring finder is research only (not built). Confirmed items, overrides, paid/late matching and user
edits behave as before; user edits always win over detection.

## Detection (`services/recurring.py`)
`classify` keeps the old rules first: amounts within 20% (`AMOUNT_TOLERANCE`), 70% of the gaps in one band,
3 charges (2 for quarterly/yearly). When those don't match, two new rules apply:
- **Monthly after 2 charges** (`_two_charges`): exactly 2 charges, in consecutive months, 27-33 days apart,
  amounts at most 5% apart (`TWO_CHARGE_TOLERANCE`), and the last one at most 40 days ago
  (`TWO_CHARGE_MAX_AGE_DAYS`; the next charge isn't a week overdue). Weekly, biweekly and semimonthly still
  need 3 charges: one 7- or 14-day gap is too often chance (groceries).
- **Amount changes** (`_variable`, utilities and phones): the last up to 6 charges (`VARIABLE_RECENT`, at least
  `VARIABLE_MIN_CHARGES` = 3) have **every** gap in the monthly (or quarterly) band, land within 5 days of the
  month of each other (`VARIABLE_DAY_SPREAD`, month ends count as the 31st), and the largest is at most 3× the
  smallest (`VARIABLE_MAX_RATIO`). An extra charge in between breaks the gaps, so a store visited more often
  is never a bill.
- Neither new rule applies when the merchant has two charges on one day ("crowded").
- **Shopping check** (new suggestions only): weekly/biweekly money out needs the same amount on at least half
  of the charges (`FIXED_SHARE`); varying weekly totals at one store or gas station are shopping, not a bill.
  Items that already exist keep being refreshed by the old rules (`classify(..., refresh=True)`).

**Name variants and moves.** `loose_key` drops noise words (`NAME_NOISE`: com, inc, llc, pos, ach, trace, pmt,
online...), words with digits and single letters: "NETFLIX.COM", "Netflix Inc" and "Netflix" are one name;
"ACH DEBIT GEICO 0612 TRACE 7919" stays apart from "... PROGRESSIVE ...". When nothing of 3+ letters is left,
the exact key is used. `merchant_key` and the stored `merchant_key` are unchanged.
- One account: exact names with the same loose key are joined when each starts after the previous one stopped
  (no overlap) and together they recur.
- Another account: a series is joined to the one on the old account when the old one stopped before the new
  one began and together they recur. Two cards charging at the same time stay two bills.
- Every join must be one period apart, give or take 3 days (`JOIN_SLACK_DAYS`), so an extra charge a few days
  later is not taken as a rename.
- A new series links to an existing item through any of its names/accounts (active, suggested or dismissed),
  so a renamed or moved bill **updates** the item and never makes a second suggestion; a dismissed item stays
  dismissed. A new series that is only a name variant of an item on the same account (or one without an account,
  e.g. "Netflix" added by hand) is not suggested either.
- New suggestions take the latest name, account and amount.

**What detection writes for an existing item.** As before: `last_seen_date` and `next_date`. New: a detected
item's amount follows the last charge, and its account follows a move, but only while they are still the values
detection wrote. Detection keeps a note per item in `app_settings.recurring_detection` (`MEMO_KEY`):
`{"<id>": {key, created, amount, account, aliases}}`. If the user changed the amount or account (PATCH), it no longer
equals the note and detection leaves it alone, always. Items from before 3.19 have no note: their amount
counts as detection's only when it equals one of the series' charges, else it is the user's for good. A note
counts only while the item's `merchant_key` and `created_at` still match (a reused id gets nothing).
Manual items never have their amount or account changed. `once` items are never touched. Price tracking
(`alerts._price`, Settings D7) obeys the same note. **Change from D7:** it never overwrites an amount the note
marks as the user's (`users_amounts`). The price event (cleared tombstone, once per charge) is still recorded as
before, and when it does follow a charge it updates the note too (`follow_amount`; the note is read once and
saved once per check). Items without a note keep the D7 behavior (the amount follows each new charge, even after
a hand edit). Amounts the note marks as the user's, and manual items, are asked about instead (see "Price changes on
hand-set amounts").

**Matching.** `aliases` are the other `(account_id, merchant_key)` pairs a joined series used (at most the 20
most recent, `MAX_ALIASES`). One owner per pair: detection never notes a pair that another item owns, or whose
key a hand-added item without an account has; `item_aliases` enforces it again when reading (a pair that is another
item's own `(account, key)` or `(none, key)`, or that a lower item id already lists, is dropped). One shared path:
`item_aliases(session, items)` (pairs on hidden accounts left out),
`match_keys(item, aliases)` (keys to index by) and `claims(item, aliases, account_id, key)` (a charge is the
item's: its own key on its account, any account when it has none, or an alias with its exact account). Each
place keeps its own rule for its own key's hidden rows, as before. Places that see aliases:
- Calendar (`calendar._Txns.for_item`): paid/late matching on the calendar, Bills left, the Budget's bills
  (`bill_occurrences`), Home's Coming up and reminders; alias rows on hidden accounts are skipped.
- `matched_history`: the category when a suggestion is confirmed, the Recurring candidates' evidence,
  Subscriptions' charges.
- Reports › Subscriptions (`subscription_list` / `_own_charges`): price change and "same price all year". A
  charge equally close in amount to two items counts for the lower id only.
- Reports bill categories (`bill_ids` / `_is_recurring`: "Where you spent the most" and bill-type categories).
- Price tracking (`alerts._price`): the last charge may be under an alias.
- Cash forecast daily spending (`forecast.daily_spend_cents`): alias charges are left out like the item's own.
- Paychecks vs transfers (`spending.transfer_items`): the newest 12 deposits across the item's own key and aliases.
An item without aliases matches exactly as before.

**Safety.** A broken note (bad or deeply nested JSON, wrong types, odd ids, bad alias entries) is ignored, never
raised: detection, the calendar, bills, Home and Reports work as if there were no note, and detection writes a
clean one. Joining across accounts is skipped for a loose name with more than 20 series (`MAX_JOIN_SERIES`).
Tests: `tests/test_recurring_detect_v2.py`, `tests/test_recurring_aliases.py`.

## Price changes on hand-set amounts (owner's decisions)
When a bill or paycheck whose amount the user set by hand gets a charge or deposit at a different amount (more than
$0.50 and more than 1%, `price_ask.changed`; checked by `alerts._price` once per charge, the same tombstone as
before), Iron Owl asks: **"Netflix now charges $17.99. Update your amount?"** (money in: **"Acme Payroll
came in at $2,410.00. Update your amount?"**) with **Yes** / **No**. Hand-set = a detected item whose
note marks the amount as the user's (`hand_set_ids`), or any manual item (its amount was typed). Active, repeating items
(not `once`). Detected amounts keep following prices silently. Charges on hidden accounts never count for price
tracking (no question, no follow), so an item without an account can't be matched to a hidden card's charge.
- **Where**: Home's needs (`price_change`, tone `warn`, key `price:{id}`, fingerprint the charge id, data
  `{recurring_id, name, amount, current, income, date, transaction_id}`, amounts positive dollars, newest first,
  at most 10). Yes / No are its buttons, plus **Not now** (`PUT /api/home/dismissals/price:{id}`, like other Home
  items): it hides the row from Home until the next charge (new fingerprint); the question stays on the item.
  On Bills and paychecks: `GET /api/recurring` items carry `price_question` (`{transaction_id, amount (signed),
  date}` or null; only this route, so snapshots are unchanged). The page shows it on the item's earliest open chip
  (an amber "New price?" tag; Item details has the question), under its List view row, in Item details and in
  Find. Amber (`--warn*`), Yes / No at least 44 px tall.
- **Yes** = `PATCH /api/recurring/{id}` `{"amount": -17.99}` (the open question's amount, signed like the item):
  the amount stays the user's (`recurring.mark_hand_set` sets the note's amount to null, even when it equals what
  detection last wrote), so the next change asks again.
- **No** = `POST /api/recurring/{id}/price-question` `{"transaction_id", "answer": "no"}` (`StrictModel3`, path id
  1..2^63-1, 204): the user's amount stays and that charge never asks again. **Every later charge at an amount different
  from theirs asks again, even the same new price**, until they say Yes or change their amount. `"answer": "undo"`
  asks again (the page's Undo). A question that is gone (answered, or replaced by a newer charge) is left alone
  (204). Unknown item: 404.
- A later charge back at the user's amount, or their own edit to the new amount, ends the question.
- **Switch**: Settings › Alerts row "Amount changes" ("When a bill or paycheck comes in at a different amount than
  the one you set"), on by default, sets `alert_settings.price` (`PUT /api/alerts/settings` `{price: {enabled}}`).
  Off: no new questions, and open ones are hidden. Never shown for an item on a hidden account.
- **Storage**: `app_settings.recurring_price_questions` = `{"<id>": {key, created, txn, cents, date, kept}}`
  (`kept` = the user's amount when they said No to that charge), at most 200 entries (newest charges), valid only while
  the item's `merchant_key` and `created_at` match. Broken JSON or entries (wrong types, `cents`/`kept` past the
  money maximum) are ignored, never raised: Home and the Recurring list show no question, and the next check
  writes a clean value. No migration.
- Bills hand-set before this release ask only about charges FinTrack hasn't seen yet (owner's decision).
- **Mock**: `&price=some` (default in `mock=full`: Comcast Xfinity now $84.99) | `none` | `lots` (also Netflix,
  Spotify and the Acme Corp paycheck). The mock's "price" switch is on, like the server's seed.
- Tests: `tests/test_price_ask.py`, `tests/test_settings_d7.py` (the switch); `scripts/recurringPanels.test.ts`
  (price questions).

## Maybe on the calendar (`GET /api/forecast/calendar`, additive)
- `ForecastOccurrence.maybe: boolean` (false for every row of `occurrences[]`).
- `ForecastCalendar.maybe: ForecastOccurrence[]`: occurrences of **suggested** items that pass the same
  calendar filter as active ones (no account, the forecast account, or a credit account), effective
  date in [max(from, today), to], sorted like `occurrences`. Each has `maybe: true`,
  `status: "upcoming"`, `counted: false`, `counted_on: null`, `carried: false`, `actual: null`,
  `override: null`, `movable: false`, `next_in_series: false`, `reminder_days: 0`; key `r{id}:{base}`,
  series `r{id}`, amount = the item's amount (the last charge).
- Maybe rows are a separate array so nothing that reads `occurrences`/`series` changes: they are not
  in `days`, `months`, `first_dip`, `series`, Bills left, pay periods, `bill_occurrences` /
  `income_occurrences`, Home, `low_ahead` or `reminder` alerts.
- **Yes** = `PATCH /api/recurring/{id}` `{"status": "active"}` (same as confirming on the Recurring
  page, including the category from history). **No** = `PATCH … {"status": "dismissed"}` (never
  suggested again). No new routes.
- An occurrence already matched to a payment (status `paid`, e.g. today's) is left out: only
  `upcoming` ones are Maybe rows.
- Which kinds show: `calendar.MAYBE_KINDS = ("in", "out", "card")`: bills, card charges and
  paychecks (owner's decision).
- **Hidden accounts**: a suggestion on a hidden account is never a Maybe row
  (`on_calendar(..., visible_only=True)`), and its `RecurringCandidate.on_calendar` is false; Find a
  bill or paycheck lists it under "Suggested, not for this calendar" (Add / Not a repeating bill).
  Active items on a hidden card keep the Release 3.6 rule (they stay on the calendar).
- **UI** (`pages/recurring/MaybeChip.tsx`): on the month grid, a day's Maybe chips come after its
  items: no fill, a 2px dashed border in the kind's color, the word "Maybe" (a neutral pill, so it
  never rides on color), the name and amount in quieter text, and two buttons **Yes** / **No**
  (at least 40×40 px; tooltip "Iron Owl thinks this repeats. Yes puts it on your calendar. No,
  and Iron Owl won’t suggest it again."). The chip itself is not a button: clicking it opens
  nothing (no Item details, no move, no Mark paid), it can't be dragged, and a double-click on it
  doesn't open Add (`data-maybe` in `isAddDoubleClick`). List view (phones, and the List choice):
  the same row layout, status line "Maybe a repeating bill · not counted yet" ("Maybe repeating
  income …" for money in), the question "Is this a repeating bill?" / "Is this repeating income?",
  and Yes / No (44 px tall, full width on phones); the day's "Then $X" stays on its last real row.
  The key under the toolbar adds "Maybe: not counted until you say Yes" when there are any.
  Printed: chips without the buttons.
- **Narrow day cells**: the grid is at least 55rem (880 px) wide, so a cell is ≥ 120.6 px (6 gaps of
  6 px), its content ≥ 102.6 px (8 px padding, 1 px border each side) and a Maybe chip's inside ≥
  86.6 px (6 px padding, 2 px dashed border each side). The chip is a size container: while its
  inside is ≤ 6.5rem (104 px) Yes sits above No, each the full width (≥ 86 px) and 40 px tall; wider
  (a grid over ~1,000 px) they sit side by side, 2 × 40 px + 6 px gap with ≥ 18 px to spare.
  Phones (≤ 640 px) always get the List view.
- **Coming up's "possible repeating charges" box stays**, but lists only suggestions for this
  calendar with no Maybe chip from today through today + 35 (`boxCandidates`); hidden when none.
  Dropping it would leave a yearly or quarterly suggestion months away answerable only by paging
  to that month (Find lists only suggestions *not* for this calendar). Suggestions on other bank
  accounts or hidden accounts are in Find; Home shows no suggestions.
- **Keyboard and screen readers**: Yes / No are ordinary buttons in tab order. Each chip is a
  group named "Maybe a repeating bill: Netflix, $15.49 on your card, Sep 28. Not counted in your
  balance yet."; the buttons are "Yes, Netflix is a repeating bill" / "No, Netflix isn’t a
  repeating bill" ("… is repeating income"). The day's hidden line counts them ("2 items, 1 maybe.").
- **After Yes / No**: the calendar reloads; focus goes to the item's new chip (Yes) or its day
  (No). Toast with Undo, the same words as the suggestions in Coming up: Yes "Netflix is on the
  calendar · every month." (label "Netflix added"); No "Iron Owl won’t suggest Netflix again."
  ("Netflix hidden"). Undo PATCHes `{"status": "suggested"}`, so it is a Maybe again.
- The client keeps `maybe` apart from `occurrences` (Bills left, Lowest point, Coming up, pay
  periods, Subscriptions and the balance chart read only `occurrences`/`days`; `billsLeft` and
  `comingUp` also drop any row with `maybe: true`), and merges them only to draw a day (`dayRows`).
- **Mock**: `&maybe=some` (default in `mock=full`: Planet Fitness on the card, GEICO and Allstate
  renters from checking) | `none` (empty `maybe`) | `lots` (six more, two on one day, a weekly one
  and money in). Yes / No PATCH the mock item, so it moves to the calendar or goes away. The mock
  leaves hidden accounts out like the server.
- Tests: `tests/test_calendar_maybe.py`; `scripts/recurringCalendar.test.ts` and
  `recurringPanels.test.ts` (Maybe sections).

# Iron Owl public release (2.0.0)

## M3. Bank connection limit is a setting

Plaid's free Trial plan allows 10 bank connections (Items), ever. Release 3.10 counted them for
everyone ("Connections used" above); now counting is a vault setting, so people on a paid Plaid
plan see no count and nothing is blocked.

- Setting `app_settings.plaid_items_cap`: "1" = count against 10, "0" = no limit. No migration.
- A vault that never saved it decides once (`services/accounts.py` `decide_items_cap_once`): on
  when it already has a bank connection or a saved `plaid_items_linked` count above 0 (an install
  from before 2.0.0), off otherwise (a new vault). The choice is saved after every unlock while
  missing (`routers/auth.py` `evaluate_after_unlock`), and before a new Item is counted, a count
  is changed or the switch is used, so a new vault's first connection can't turn it on. A GET
  never writes: it answers with the choice that would be saved.
- The lifetime count (`plaid_items_linked`) keeps going up either way, so turning counting on
  later shows the real number (it can be above 10; then "all used" shows).
- `GET /api/plaid/status` adds `items_cap: bool`; `items_limit` is 10 when on and 0 when off.
- `PUT /api/plaid/items-cap {"on": bool}` (session, `X-FinTrack`, strict body; anything else
  422) → `{items_cap, items_linked, items_limit, items_now}`. `PUT /api/plaid/items-linked`
  answers 409 while counting is off, and its answer adds `items_cap`.
- Nothing on the server blocks linking at the limit; the app's checks (`components/bankSlots.ts`
  `slotsOf`) treat `items_cap: false` or `items_limit` 0 as "no limit": no counter, no "all used"
  message, and the Remove window leaves out "one of your limited bank connections".
- Settings › Banks › Manage banks, "Linked institutions": a switch "Count bank connections
  (Plaid's free Trial plan allows 10, ever)". While on, "Bank connections used: N of 10" and
  "Change…" show above it as before.
- Mock: `mockAccounts.ts` `&cap=on|off` (default on); the switch and the 409 work in the mock.
- Tests: `tests/test_plaid_cap.py` (new vault off, old vault with a connection or a saved count
  on once, the switch, strict body, session and header, 409, linking past 10 while off);
  `scripts/accounts.test.ts` (counting off shows nothing and blocks nothing).

## M5. Help page
- `HELP.md` at the repo root is the one source of help text, for GitHub readers and the app. Plain
  words for a non-technical reader: what Iron Owl is, the privacy line ("Your data stays on this
  PC. Iron Owl only talks to Plaid, to fetch your bank data, and to GitHub, to see if a new version
  is out. The update check sends nothing about you or your money."), who to call, asking Claude for
  help (never share the password, recovery sheet or Plaid keys), installing on Windows (including
  "Windows protected your PC": More info, then Run anyway), getting your own Plaid keys, the
  recovery sheet, backups, updates, and locking. No "FinTrack" in it (a unit test checks).
- Route `#/help` (`pages/Help.tsx`, lazy). The text is bundled at build time with
  `import helpText from '../../../HELP.md?raw'`, so Help works offline and needs no backend route.
  `vite.config.ts` sets `server.fs.allow: ['.', '../HELP.md']` so the dev server may read that one
  file above `frontend/`; `npm run build` bundles it into the Help chunk. `start.ps1` rebuilds the
  frontend when `HELP.md` is newer than the build.
- Renderer (`lib/helpMarkdown.ts`, `parseHelp`): `#`/`##`/`###` headings (deeper ones become level
  3), paragraphs, `-`/`*` lists, numbered lists (keeping the first number), indented continuation
  lines, `**bold**`, `[text](link)` and backslash escapes. Output is a tree rendered as React text:
  no raw HTML and no `dangerouslySetInnerHTML`, so tags like `<script>` show as typed. Links keep
  only `http(s)://` addresses (new tab, `rel="noopener noreferrer"`, an external icon and "(opens in
  a new tab)" for screen readers) and in-app `#/` routes (a router `Link`); any other link
  (javascript:, data:, relative, `#anchor`) is dropped and its words stay as text.
- "Who to call" card on top: "Need help? Ask {contact}." from `support_contact` in
  `GET /api/auth/status` (`authInfo.supportContact` in `state.tsx`; readable while locked; the same
  value the M7 Settings field and installer page set). Hidden when no one is named.
- Reachable from: the main menu (sidebar and the small-screen Menu sheet; `help` icon, after
  Settings), the Ctrl+K search ("Help"), a "Help" link under "Forgot your password?" on the
  password screen, a "Help" link on first-time Setup (create and restore cards; not on the
  recovery sheet step, which can't be shown again), and one on the "isn't running" screen. That
  last one shows only when Help's code is already loaded: the Gate loads it early
  (`pages/helpModule.ts`, `lib/preload.ts`, tested in `scripts/preload.test.ts`) because a
  stopped server can't send it. Mock: `?mock=setup`, `?mock=offline`. While not unlocked (loading, locked, setup, already open elsewhere, not running)
  the Gate in `App.tsx` shows `#/help` on its own page (`HelpScreen`) with a Back button at the top and bottom
  (back to where it was opened from, else `#/`). Unlocked, it shows inside the app frame. Update
  install screens still win over Help.
- Mock: nothing new (no backend route). The card follows the existing `support=` mock param
  (`support=none` hides it), e.g. `?mock=locked#/help`.
- Tests: `scripts/helpMarkdown.test.ts` (parsing, unsafe links dropped, `<script>`/HTML as text,
  and HELP.md itself: required sections, only safe links, no "FinTrack").

## M6. Updates from GitHub

Iron Owl installs made from the public GitHub release update themselves from GitHub Releases. Same signed-file pipeline as the emailed updates (`# Private updates (D5)`): the check finds the newest release's `.ftupdate`, downloads it, verifies it, offers it, and the same install job installs it. The owner's and family installs keep the emailed `.ftupdate` flow and never go online for updates.

**Privacy (owner-approved wording; README, Help):** "Your data stays on this PC. Iron Owl only talks to Plaid, to fetch your bank data, and to GitHub, to see if a new version is out. The update check sends nothing about you or your money."

### Update source (`backend/app/updates/source.py`, decided once at startup)
- `file`: emailed files found in Downloads or picked in Settings (as before). `github`: GitHub Releases. `off`: no updates.
- The build decides: each version's `release.json` may carry `update_source` (`"file"`|`"github"`) and, for github, `update_repo` (`owner/name`, from the build, e.g. the workflow's repository; never written in the code). Missing = `file` (every install made before 2.0.0). GitHub builds write both keys; emailed-file builds write neither (versions before 2.0.0 refuse any release.json key beyond the six known ones, so a file build that wrote even `"update_source":"file"` could not be installed by them).
- `update_repo` must match GitHub's rules: owner 1-39 letters/digits/single inner hyphens; name 1-100 of `A-Za-z0-9._-`, not `.`/`..`, not ending in `.git`. Invalid or missing for github → `off`.
- `UPDATE_SOURCE=file|github|off` in the settings file overrides the build (case and spaces ignored). Any other value → `off` (an unexpected value never turns the network on). `github` still needs `update_repo` from release.json.
- Only an installed copy (mode `enabled`) has a source; git, dev and not-installed copies are `off` whatever the build or settings say.
- `extract_payload` accepts the two optional keys (`package.OPTIONAL_RELEASE_KEYS`): `update_source` absent, `"file"` (no repo), or `"github"` with a valid repo; anything else fails the install (FT-UPD-02).
- With source `off` every update action answers 409 `disabled` and status shows no offer; an install already under way still finishes (unlock hook, automatic and manual rollback don't depend on the source).

### Keys (`backend/app/update_keys.py`)
- `TRUSTED_KEYS` (ids `ft-YYYYx`): the file source. `GITHUB_KEYS` (ids `io-YYYYx`): the github source, shipped EMPTY. `keys_for(source)`: file → only `TRUSTED_KEYS`, github → only `GITHUB_KEYS` minus any id or key also in `TRUSTED_KEYS`, anything else → none. Picked files, the check, the install's re-verification and extraction all use the source's set.
- Empty GitHub set: the check stops before any network request with the `no_keys` message; nothing downloads.
- Owner step (once): make the public key pair with `tools/release/keygen.py` (id `io-2026a`), keep the private key only as the release workflow's repo secret, paste the public key into `GITHUB_KEYS` and commit it.

### The check (`backend/app/updates/github.py`; `Updater.check_github`)
1. No GitHub keys → `no_keys`.
2. `GET https://api.github.com/repos/{repo}/releases/latest` (headers: `Accept: application/vnd.github+json`, `User-Agent: Iron-Owl-updater`, `X-GitHub-Api-Version`). 404 = no release yet (no error); 403/429 → `busy`; 5xx or no answer → `offline`; anything else → `bad_answer`. Answer capped at 1 MB and 60 s in all (else `offline`), parsed defensively (object, `tag_name` = `vX.Y.Z` or `X.Y.Z`, `assets` a list). Drafts and prereleases are ignored.
3. The update asset is the one named exactly `Iron-Owl-X.Y.Z.ftupdate` for the tag's version (other assets, e.g. the installer, are ignored; none = no update, no error). Its `size` must be an integer within 200 B..150 MB (over → `too_large`), its `browser_download_url` exactly `https://github.com/{repo}/releases/download/{tag}/{name}` (case-insensitive, no query).
4. Only when X.Y.Z is newer than this version and newer than the current offer: if this exact asset (name, size, asset id) was refused before, `rejected` again without downloading; installed/failed before → nothing.
5. Download into `data/updates/.incoming-<hex>/`: every hop (the asset address and each redirect) must be HTTPS to `api.github.com`, `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` or `github-releases.githubusercontent.com` (exact host, port 443, no user:password, no spaces or control characters); redirects are followed by hand, at most 5. `Content-Length` that isn't 1-12 ASCII digits → `bad_answer`; over 150 MB → `too_large`; different from the asset size → `bad_answer`. The stream may not pass the declared size (nor 150 MB). Nothing is written to disk before the first block (magic, section lengths, signature with the GitHub keys: `package.check_head`) passed; a refused head stops the download (`rejected`, remembered). Wrong final size → `bad_answer`. 30 s socket timeouts, 20 minutes in all; reads take what has arrived (`read1`), so a slow trickle can't hold one read past the deadline.
6. The copy is verified in full with the GitHub keys (`check_staged_file`, as for Downloads files) and its manifest version must equal the tag's (else `rejected`); then it becomes the offer (`source: "github"`, `file_name` = the asset name) under the offer lock. The incoming folder is always removed.
- Proxy: the update check uses Windows' proxy settings, like a web browser (owner decision; no setting of its own).
- Requests carry no cookies (no cookie jar), no auth, nothing about the user or their money. Errors are stored as a code and shown as plain words; any unexpected error in a check is recorded as `bad_answer` (so it is retried after the usual wait, and "Check now" never answers 500); exception text is never shown or logged (logs carry the code or the exception type).
- Never while locked, never while an install runs, one check at a time.

### When it runs
- Automatic, once a day (switch on, github source, unlocked): from the 5-minute automation tick and from the window's own scans (on unlock and focus). Due when there was no try yet, 24 h after the last try, 3 h after a failed one, or when the last try lies in the future (clock moved back).
- "Check now": `POST /api/update/scan {"now": true}` checks right away, also with the switch off; at most once a minute (a quicker repeat answers with the last result).
- Stored in `data/updates/state.json` (not the vault: no amounts, readable by the updater without a database session) as `check: {auto, last_check, last_try, error}`; every field re-validated on load (`error` one of the codes below).

### Routes (changes)
- `GET /api/update/status` adds `source` (`file`|`github`|`off`), `auto_check` (bool; false outside github), `last_check` (ISO UTC of the last check that finished, github only, else null), `check_error` (plain words, github only, else null).
- `POST /api/update/scan` takes `{}`, no body, or `{"now": true}` (StrictModel3, strict bool). File source: Downloads scan as before (`now` ignored). GitHub source: the check when due, or now with `now`.
- New `PUT /api/update/auto-check` `{"on": bool}` (StrictModel3, strict bool; session; `X-FinTrack`) → UpdateStatus. 409 `disabled` unless the source is github.
- `POST /api/update/file` and every action answer 409 `disabled` with source `off`. A picked file is verified with the source's keys.
- UpdateOffer `source` adds `"github"`.

### Messages (`check_error`)
| code | words |
|---|---|
| `no_keys` | Updates can’t be checked yet. This copy of Iron Owl doesn’t have the key it needs to make sure an update is real. |
| `offline` | Iron Owl couldn’t reach GitHub to check for updates. It will try again later. |
| `busy` | GitHub asked Iron Owl to wait before checking again. It will try again later. |
| `bad_answer` | GitHub’s answer didn’t look right, so nothing was downloaded. Iron Owl will try again later. |
| `too_large` | The new version’s file is too big to be an Iron Owl update, so it wasn’t downloaded. |
| `rejected` | The new version didn’t pass Iron Owl’s safety check, so it wasn’t installed. Your data wasn’t changed. |
| `disk` | Iron Owl couldn’t save the new version on this PC. It will try again later. |

### Frontend
- Banner, GitHub offer: "A new version of Iron Owl is ready to install." (the rest as before: "Version X · takes about a minute", Install update / What’s new / Not now).
- Settings › Colors, text size and updates › Updates, github source: "Iron Owl looks on GitHub once a day for a new version. If one is out, it downloads it, makes sure it really comes from Iron Owl, and asks you before installing.", "Last checked: Today|Yesterday|<date>|Not yet", the problem in amber when `check_error` is set, an outline "Check now" button ("Checking…" while it runs; "Checked just now." when nothing new), and a row "Check for updates once a day" (help: "Only while Iron Owl is open and unlocked. The update check sends nothing about you or your money.") with the On/Off switch (message with Undo). "Install from a file" stays. Source off: "Updates are turned off for this copy of Iron Owl." and no actions.
- Mock: `update=github | github-found | github-new | github-error | github-nokeys | github-off | sourceoff` (`frontend/src/dev/mockUpdates.ts` header).

## M4. Name and icon: Iron Owl

- **Name**: the app is called **Iron Owl** everywhere a person can see it: window and tab titles,
  on-screen text, the web app manifest (`name` and `short_name`), Plaid Link's app name
  (`CLIENT_NAME` in `backend/app/plaid_client.py`), the installer, desktop and Start menu shortcuts,
  the Settings > Apps entry, messages from the server that reach the screen, and backup file names
  offered on save or download. Every install shows "Iron Owl"; there is no build setting for the
  old name.
- **Internal names stay** (renaming them could break real vaults and existing installs): folders
  (`%LOCALAPPDATA%\FinTrack`, `Programs\FinTrack`), `FINTRACK_*` variables, `fintrack.env`,
  `fintrack.db`, `keyfile.json`, `.ftbackup` / `.ftupdate` extensions, the update file's magic bytes
  and signing label, `FT-` codes, the `X-FinTrack` header, the `ft_session` cookie, saved-setting
  keys, `fintrack-auto-*` backup names, logger and package names, and code comments.
- **Icon**: a dark owl with a keyhole on its chest, on an amber rounded square. The one source is
  `packaging/brand/iron-owl.svg` (512 × 512, no `<metadata>`); `python tools/brand/make_icons.py`
  (stdlib only, run from PowerShell; rasterizes with the PC's Microsoft Edge in headless mode)
  makes every other file from it, and `--from <svg>` cleans in a new logo first:
  - `frontend/public/favicon.svg` (the source), `favicon.ico` (16, 32, 48),
    `apple-touch-icon.png` (180, full-bleed: iOS rounds it), `iron-owl-192.png` / `-512.png`
    (rounded tile, manifest purpose `any`) and `iron-owl-maskable-192.png` / `-512.png`
    (full-bleed amber, the owl inside the 80% safe circle, purpose `maskable`).
  - `packaging/brand/iron-owl.ico` (16, 24, 32, 48, 64, 128, 256; 256 stored as PNG) for the
    launcher, shortcuts, installer and the Settings > Apps entry.
  - `packaging/brand/wizard-image.bmp` (164 × 314) and `wizard-small.bmp` (55 × 58), 24-bit, for
    the installer's `WizardImageFile` and `WizardSmallImageFile`.
  - At 16 and 24 px the owl is drawn larger and without the eye highlights and beak, which only
    blur into gray at that size.
  - **No metadata**: every PNG (also the 256 px one inside each `.ico`) is re-encoded from its
    pixels with only `IHDR`, `IDAT` and `IEND`; SVGs have no `<metadata>`, comments, `<title>` or
    `<desc>`. The script checks this after writing (the export leak check scans image metadata).
- **In the app**: the sidebar, top bar and sign-in screens show the owl logo itself
  (`components/OwlLogo.tsx`, fixed colors, the same in every theme) instead of the old trend-line
  mark; the "Starting Iron Owl…" screen in `index.html` and `StartingScreen.tsx` uses the same
  drawing.
- **File names offered on download**: `iron-owl-backup-YYYY-MM-DD.ftbackup` and
  `iron-owl-transactions-YYYY-MM-DD.csv` (server `Content-Disposition` and the browser save).
- **Suggested backup folder**: "Iron Owl backups" (OneDrive, else Documents). A "FinTrack backups"
  folder made before 2.0.0 is suggested instead while there's no "Iron Owl backups" there, and a
  saved location in it keeps working with no steps (folder names are never checked).
- **Weak passwords**: the strength meter also counts "ironowl" / "iron owl" as a common word.
- **Windows that are already open**: the launcher finds the app window by its title; it accepts
  both "Iron Owl" and "FinTrack" (M7). An install updated only by `.ftupdate` keeps its old
  launcher, so with the app open its shortcut opens a second window (the "open in another
  window" screen) until the 2.0.0 installer has run once.
- Tests: `tests/test_settings_d7.py` (`test_suggested_folder`,
  `test_older_fintrack_backups_folder_keeps_working`), the renamed message assertions across the
  backend tests, and `scripts/gate.test.ts`, `updates.test.ts`, `reportsSpending.test.ts`.

## M7. Installer and GitHub build

- **Version**: 2.0.0 for every install (`backend/app/version.py` is the one source;
  `frontend/package.json` and its lock carry the same number, `tests/test_installer.py` checks).
  `RELEASE_NOTES.md` starts fresh at `## 2.0.0`.
- **Package** (`tools/release/build_package.ps1`): folder `dist-package/Iron-Owl-<v>/` (zip
  `Iron-Owl-<v>-install.zip`). Ships `THIRD_PARTY_NOTICES.md` (and `LICENSE` when present) and the
  Iron Owl icon (`packaging/brand/iron-owl.ico`, staged as `FinTrack.ico`). New: `-RequireLock`
  (refuse without `tools/release/requirements-lock.txt`), `-UpdateSource file|github` and
  `-UpdateRepo <owner>/<name>`. Only github builds add `update_source: "github"` and
  `update_repo` to `versions/<v>/release.json`; file builds (the owner's) add no keys, because
  installed versions before 2.0.0 refuse unknown release.json keys.
- **Runtime lock** (`tools/release/make_lock.py`): wheels only, hash-pinned, except an explicit
  allowlist of sdist-only packages (just `plaid-python`, which publishes no wheels); any other
  sdist is refused. The lock pins that sdist's sha256 (`# sdist:` header) and
  `tools/release/build-lock.txt` pins its build backend (setuptools, wheels, same deps_id).
  `make_lock.py install-runtime` (used by `build_package.ps1` and `build_update.py --kind full`)
  builds the sdist in a fresh venv holding only that backend (`pip wheel --no-deps
  --require-hashes --no-build-isolation`; must give one `py3-none-any` wheel), then installs the
  lock with `--require-hashes --no-deps --only-binary=:all:` and `--find-links` to that wheel
  (its pin takes the built wheel's sha256). Works on a clean GitHub runner: no prepared wheels.
- **install.ps1** gains a non-interactive mode for the setup .exe: `-Unattended` (never asks;
  a missing or blank contact keeps the saved one), `-SupportContactFile <utf-8 file>` (the name
  never goes on a command line), `-LogFile` (a transcript; it never holds the contact), `-ResultFile` (gets `ok` as the last step). The
  interactive path is unchanged. A given contact goes to `state.json` and to
  `data/support_contact.json`. Shortcuts are named "Iron Owl" (an older "FinTrack.lnk" that
  starts this install is replaced; a desktop icon the person had is kept). Settings > Apps shows
  "Iron Owl" (registry key `...\Uninstall\FinTrack` stays). `uninstall.ps1` is unchanged in what
  it does (data kept unless `-RemoveData` and typing DELETE); it removes both shortcut names.
  Settings > Apps runs it with its window hidden: it asks with a Windows Yes/No box that says the
  data is kept, says "Iron Owl was removed" in a box when done, and shows any failure in a box
  (never a typed answer). `-RemoveData` (PowerShell window only) and `-Yes` keep the console.
- **Setup .exe** (`installer/iron-owl.iss`, Inno Setup 6.3+, `installer/build_installer.ps1`):
  per user, no administrator rights (refuses to run elevated, in plain words), unsigned for
  2.0.0. Pages: welcome; "Who to call for help" (optional, at most 60 characters, control and
  invisible characters refused); "Put an Iron Owl icon on the desktop" (on); ready; finish
  ("Open Iron Owl now"). It unpacks the package into Setup's private temp folder and runs
  `install.ps1 -Unattended` hidden; no Inno uninstaller and no second Settings > Apps entry.
  On failure: a plain message with the log's path (`%TEMP%\Iron-Owl-install.log`), the finish
  page says so, and Setup exits with 1. Output `dist-installer/Iron-Owl-Setup-<v>.exe` + `.sha256`.
- **Who to call for help** (Settings > Safety and backups): `PUT /api/settings/support-contact`
  `{"contact": str}` (session + `X-FinTrack`; StrictModel3; trimmed; at most 60 characters; no
  control or invisible characters, else 422 without echoing the value) returns
  `{"support_contact": str | null}`. Saved in `data/support_contact.json` (plain file next to the
  vault, like `limiter.json`, so it is readable while locked); when present it wins over
  `SUPPORT_CONTACT` / `state.json` (an empty value means no one is named). It is read at startup
  and applied at once; `GET /api/auth/status` and `/api/update/status` `support_contact` show it.
  The launcher's "couldn't start" box reads the same file first. Mock: `mockSettings.ts`
  (`&contactsave=fail`). Known limit: an install updated only by `.ftupdate` keeps its old
  launcher (it reads `state.json` only, and finds the open window only by the title "FinTrack")
  until the 2.0.0 setup runs once; the new launcher accepts both titles.
- **Update files**: `Iron-Owl-<v>.ftupdate` for both sources. `build_update.py --source github
  --update-repo <owner>/<name> --tag v<X.Y.Z> --passphrase-stdin`: HEAD must be the tag's commit,
  the version must equal version.py (no bump), signs with an `io-` key from `GITHUB_KEYS` and
  self-verifies with `GITHUB_KEYS` only (a `ft-` key is refused), release.json gets the two
  source keys, no EMAIL.txt and no releases.json. The passphrase comes on standard input, never
  a command line or the environment. A GitHub build always runs in two steps: `--unsigned-out
  <folder>` (no key; refused without it) and `--sign-only <folder>` (checks the folder with the
  app's own manifest parser, the payload's size and SHA-256, release.json's version, source and
  repository, and that HEAD is the tag; then signs). `keygen.py --key-id io-YYYYa` makes the GitHub key
  (`github-signing.pem`); it never replaces the emailed-update (`ft-`) key.
- **GitHub Actions** (run only when `github.repository == 'iron-owl-app/Iron-Owl'`; workflow and job
  `permissions: contents: read`, the release job alone `contents: write`; no
  `pull_request_target`; official `actions/*` only, each pinned to a full commit SHA with its tag in a comment; how to update a pin is at the top of each workflow):
  `tests.yml` on every push and pull request (windows-latest, Python 3.11, Node 24): backend
  pytest, launcher tests, `npm ci`, `npx tsc --noEmit`, `npm run test:unit`,
  `python tools/repo_map.py --check`. `release.yml` on a `v*` tag, in three jobs so the signing
  key never shares a machine with tests, npm or the web build (third-party code):
  - `build` (windows-2022, where Inno Setup 6 is preinstalled; no secrets): checks the tag against
    version.py, downloads the embeddable Python named in `tools/release/python-embed.json` (its
    sha256 is checked), builds the UNSIGNED update (`build_update.py --unsigned-out`, `--kind
    full`: `manifest.json` + `payload.bin`), the package (`-RequireLock -UpdateSource github`)
    and the .exe; uploads them as artifacts.
  - `sign` (windows-2022, `environment: release`, which holds the two secrets): a fresh clean
    checkout of the tag, Python 3.11, then only `cryptography`, `cffi` and `pycparser` as exact
    hash-checked wheels from the lock (`make_lock.py subset`), installed before any secret
    exists. One step takes the secrets out of its environment, writes the key to a user-only
    folder under `RUNNER_TEMP` (masked), pipes the passphrase into `build_update.py --sign-only`
    (which checks the folder, signs, self-verifies and runs no program but git) and deletes the
    folder in a `finally`, with an `always()` backstop step.
  - `release` (ubuntu, the only `contents: write` job, runs no repository code): makes a DRAFT
    release with `Iron-Owl-Setup-<v>.exe`, `Iron-Owl-<v>.ftupdate` and `SHA256SUMS.txt` (gh CLI,
    `GITHUB_TOKEN`). A maintainer publishes it.
  - Owner setup of the `release` environment (reviewers, `v*` tags only, a tag ruleset):
    `tools/release/GITHUB_RELEASE.md`.
- **Third-party notices**: `THIRD_PARTY_NOTICES.md` (Python, every runtime package with version
  and license, the bundled JavaScript libraries, the IBM Plex Sans font), installed with the app.
- Tests: `tests/test_installer.py`, `tests/test_support_contact.py`,
  `tests/test_release_tools.py` (GitHub builds), `packaging/launcher/tests/test_launcher_contact.py`.
