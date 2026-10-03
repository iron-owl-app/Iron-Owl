# Where problems usually start

The fragile places: read the matching entry before changing one of these areas. Moved out of `CLAUDE.md` so it loads only when needed.

- Plaid sync: `services/sync.py` `_carry_over_pending` - a posted transaction gets a new id; user edits
  (category, transfer, notes, splits, tags) must move from the pending row.
- Sync side effects: `services/sync.py` `after_sync` (balance backfill, rules, recurring detection,
  alerts; each step commits alone) and `services/automation.py` `sync_tick` (automatic sync, unlocked only).
- Envelope math: `services/spending.py` `_run`/`_carryover`/`Book` - carry-over between months,
  pending counts as spent, refunds net out; `budget_month` builds the page.
- The over-assign rule: `services/spending.py` `save_budget_cents` - a save may not lower Ready to
  Assign below zero, but saves that raise it are always allowed (so an over-assigned budget can be fixed).
- Beginner setup: `services/budget_setup.py` `run_setup` (one transaction) and `services/automap.py`
  (Groceries / Eating out / Gas auto-sorting via `app_settings.budget_auto_map`).
- Transfers: `is_transfer` + `transfer_source` (`user` > `rule` > `auto`). `auto` = a credit card payment
  (`services/card_payments.py`, set by `rules.resolve`/`apply_all` and the sync; the bank side only when it
  pairs with the payment on a visible card, never by name); hand-set categories and
  splits clear it (`rules.release_hand_set`), so every new "set the category by hand" path must call that.
  Every total leaves `is_transfer` rows out; recurring detection keeps `auto` money out (card bills).
- "Needs a category": `services/txns.py` `needs_category` and its SQL twin `needs_category_clause`
  must agree; splits rescale with `rescale_splits` when an amount changes.
- Recurring and forecast: `services/calendar.py` `_generate` is the single source of occurrences
  (calendar, bills on the Budget page, reminders); paid/late matching lives there too.
- Recurring detection: `services/recurring.py` `detect`/`classify` (cadence guesses) and the one-time
  `backfill_categories_once` (flag in `app_settings`).
- Backup folder: `services/automation.py` `validate_dir` (local, absolute, writable, outside `data/`;
  checked lexically before any disk access, since touching a UNC path leaks the Windows login hash);
  uploaded backups: `services/backup.py` `extract_backup`.
- Vault and sessions: `security.py` (1700+ lines) - lock hooks, session hand-off between windows
  (`presence.py`, `routers/app.py`), rate limits shared by password and recovery sheet.
- Migrations: `migrations.py` - one transaction per step; a failed step with an update pending asks
  the launcher to roll back (`updates/service.py`).
- Home: `services/dashboard.py` (`GET /api/dashboard`) returns per-section `errors` (cash, budget,
  coming_up, months, goals, investments, needs); a failing section must not break the page. The
  budget month and the calendar (today..today+35) are built once and shared; "Not now" stays at
  `/api/home/dismissals/{key}` (`routers/home.py`). Live needs are filtered by the Settings switches.
- Frontend: `api.ts` `request` (401 -> locked, `quiet401`/`probe` for polling), `state.tsx` phases,
  `pages/transactions/useTxnPages.ts` (keyset cursor paging), `pages/spending/BudgetDetailed.tsx`
  (largest page, ~1900 lines).
- Static files: `main.py` `_static_candidate` rejects UNC/drive paths before touching the disk.
- Folder picker: `services/folder_picker.py` opens the native COM dialog on a worker thread (one at a
  time, closed on every lock); never open it in tests (`tests/conftest.py` stubs `native_dialog`).
- Undo of deletes: `routers/categories.py` `get_snapshot`/`restore_category` and
  `routers/category_groups.py` `restore_group` must put back everything a delete touched (budgets,
  rules at their positions, splits, ids); add new category-linked data to the snapshot too.
