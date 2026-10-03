# Finding things

Naming traps and where features really live. Start at `docs/MAP.md`. Moved out of `CLAUDE.md` so it loads only when needed.

- Start at `docs/MAP.md`: "Features index" lists every route; search it for a path, a function
  name or a page file. Naming traps: the Budget page is route `/spending` (`pages/Spending.tsx` ->
  `pages/spending/BudgetSimple.tsx` + `BudgetDetailed.tsx`) and router `budgets.py` (which also serves
  `/api/spending/review`); Investments = `holdings.py`; `/api/forecast*` lives in `recurring.py`;
  `/api/networth/history` in `summary.py`; `/api/debt/plan` in `loans.py`; `/api/restore` in
  `backup.py`; `PUT /api/transactions/{id}/tags` in `transactions.py`, not `tags.py`; model
  `TxnCategory` is table `categories`, and `Budget.category` holds a category id string.
- Settings (D7): the old Rules & alerts page is gone. Rules and Alerts are tabs in Settings
  (`/settings?tab=cat|rules|pay|alerts|banks|safe|app`; `/rules` redirects to `?tab=rules`). Each tab
  is `pages/settings/<Name>Tab.tsx`, listed in `SETTINGS_TABS` in `pages/Settings.tsx`. Bank
  connections, Plaid keys and auto-sync live on their own page `/settings/banks`
  (`pages/SettingsBanks.tsx`). `routers/preferences.py` serves `/api/settings` (auto-lock etc.,
  stored via `services/prefs.py`) and `/api/paychecks`, not `settings`-named routers.
- Release 3.10: "Spending" (look-back) is the Reports tab `/reports?tab=spending` (`pages/reports/SpendingTab.tsx`,
  `GET /api/reports/spending`), NOT the Budget page `/spending`. Reports tabs: overview|spending|debt (Year in
  review was removed). The Loans & credit page is gone: `/loans` redirects to `/reports?tab=debt`
  (`pages/reports/DebtTab.tsx`; debt routes and `services/debt_budget.py` live in `routers/loans.py`). The
  Budget's "Paying off debt" line is loans only; card payments are refused on every path. Accounts:
  `/accounts/:id` (`pages/accounts/*`); an account's nickname is `accounts.name`, the bank's own name and
  card limit are in `app_settings.account_facts` (`services/account_facts.py`). Card payments are
  transfers only when paired with a visible card's payment (`services/card_payments.py`).
- `BudgetMonth` (`budget_months.pool_cents`) is the legacy Release 2 pool, read only by
  migration v3; the envelope budget is `budgets` rows (`limit_cents` = the month's plan).
- SPEC.md: `grep -n "^#" SPEC.md` lists the release headings; later releases override earlier
  ones (see "Release 2.1 amendments"). Read the section for the feature before changing behavior.
  "Spending definitions" (near the top of Release 2) is what counts as spending everywhere.
- Design handoffs (the design notes SPEC.md mentions) aren't in the repo: they can contain real
  account names and amounts. Never commit one (add each new handoff folder to `.gitignore`).
