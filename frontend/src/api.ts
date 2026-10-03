/**
 * Single fetch wrapper for the FinTrack backend (see SPEC.md "API contract").
 *
 * - credentials: "same-origin" so the HttpOnly `ft_session` cookie rides along
 * - `X-FinTrack: 1` on every request (required on every non-GET for CSRF)
 * - `X-FinTrack-Session: <token>` on every request once unlocked (pairs with the cookie)
 * - JSON content-type when there is a body
 * - throws ApiError; any 401 flips the global "locked" state
 *
 * Money in every response is a number in dollars. Transaction amounts use
 * the app's sign convention: negative = money out, positive = money in.
 */

import { getTabId } from './lib/tab';

// ---------------------------------------------------------------- models

export type Category = 'bank' | 'hsa' | 'retirement' | 'investment' | 'loan' | 'credit' | 'other';
export const CATEGORIES: Category[] = ['bank', 'hsa', 'retirement', 'investment', 'loan', 'credit', 'other'];
export const LIABILITY_CATEGORIES: Category[] = ['loan', 'credit'];

export type ItemKind = 'bank' | 'investment' | 'loan';
export type ItemStatus = 'ok' | 'login_required' | 'error' | 'pending';

export interface AuthStatus {
  initialized: boolean;
  unlocked: boolean;
  auto_lock_minutes: number;
  /** Recovery sheet (no session needed): whether this vault has one, and its number ("482-913"). */
  recovery: { available: boolean; sheet: string | null };
  /** Seconds left in a "too many tries" wait (password and sheet share it), else null. */
  retry_after: number | null;
  /** Wrong tries left before a wait starts (5 when none; 0 during and after a wait). Older servers omit it. */
  tries_left?: number;
  /** Who to call for help ("Sam"); null when the install doesn't set one. */
  support_contact: string | null;
  /** FinTrack is open (unlocked) in another window that answered recently, and this one has no session. */
  open_elsewhere?: boolean;
  /** Why the vault last locked (null on a fresh start). Older servers omit both. */
  lock_reason?: ServerLockReason | null;
}

// ---------------------------------------------------------------- updates (D5)

/** How this copy of FinTrack updates: by itself (`enabled`), with git (`git`/`dev`), or not at all. */
export type UpdaterMode = 'enabled' | 'git' | 'dev' | 'not_installed';
/**
 * Where this installed copy gets updates (Iron Owl 2.0.0): `file` (a signed .ftupdate sent by
 * email), `github` (GitHub Releases, checked once a day), `off` (no updates).
 */
export type UpdateSource = 'file' | 'github' | 'off';
export type UpdateRejectReason = 'signature' | 'not_update' | 'damaged' | 'too_large';
export type UpdateHelpReason = 'reinstall' | 'python' | 'launcher' | 'skipped_version' | 'runtime_missing' | 'disk_space';
export interface UpdateOffer {
  id: string;
  version: string;
  display_version: string;
  released_at: string;
  /** 1-6 plain lines for What's new (shown as text, never HTML). */
  notes: string[];
  kind: 'app' | 'full';
  /** 1 → "about a minute", 3 → "a few minutes". */
  minutes: number;
  size_bytes: number;
  source: 'downloads' | 'picked' | 'github';
  /** Leaf name only (no folder). */
  file_name: string;
  can_install: boolean;
  help: { reason: UpdateHelpReason; code: string } | null;
}
export type UpdateStep = 'backup' | 'install' | 'restart';
export interface UpdateProgress {
  id: string;
  to_version: string;
  step: UpdateStep;
  state: 'running' | 'restarting' | 'failed';
  code: string | null;
  at: string;
}
export interface UpdateResult {
  outcome: 'installed' | 'failed' | 'rolled_back';
  from_version: string;
  to_version: string;
  code: string | null;
  at: string;
  backup_kept: boolean;
}
export interface UpdateStatus {
  mode: UpdaterMode;
  support_contact: string | null;
  current: { version: string; display_version: string; installed_at: string | null; last_update_at: string | null };
  offer: UpdateOffer | null;
  banner: { show: boolean; remind_after: string | null };
  install: UpdateProgress | null;
  last_result: UpdateResult | null;
  rollback: { available: boolean; to_version: string | null };
  source: UpdateSource;
  /** github only: Settings "Check for updates once a day" (false in the other sources). */
  auto_check: boolean;
  /** github only: ISO UTC of the last check that finished; null before the first. */
  last_check: string | null;
  /** github only: why the last check stopped, in plain words (show as text); null when fine. */
  check_error: string | null;
}
export type UpdateRejectCode = 'FT-UPD-SIG' | 'FT-UPD-NOTUPD' | 'FT-UPD-DMG' | 'FT-UPD-BIG';
/** POST /api/update/file: what FinTrack made of a picked file. */
export type UpdateCheck =
  | { result: 'ready'; offer: UpdateOffer }
  | { result: 'rejected'; reason: UpdateRejectReason; code: UpdateRejectCode; file_name: string }
  | { result: 'older' | 'same'; code: 'FT-UPD-OLD' | 'FT-UPD-SAME'; file_name: string; file_version: string; current_version: string }
  /** A file that already failed to install: FinTrack won't try it again. */
  | { result: 'failed'; code: string; file_name: string; file_version: string; current_version: string };

// ---------------------------------------------------------------- app window (launcher, presence)

/** Why the server last locked the vault. */
export type ServerLockReason = 'idle' | 'closed' | 'manual' | 'moved' | 'shutdown';
/** GET /api/health: no session, never touches the vault. */
export interface Health {
  app: 'fintrack';
  version: string;
  boot_id: string;
  window_open: boolean;
  web: boolean;
}
/** POST /api/app/alive: the heartbeat's answer (not counted as activity). */
export interface Presence {
  open_elsewhere: boolean;
  unlocked: boolean;
  lock_reason: ServerLockReason | null;
  boot_id: string;
}
/** Why a session route said 401: the vault locked, or another window took the session over. */
export type SessionEnd = 'locked' | 'moved';

export interface OkResponse {
  ok: true;
}

/** setup / unlock / change-password: the token to echo in `X-FinTrack-Session`. */
export interface SessionResponse extends OkResponse {
  session_token: string;
}

/**
 * A recovery sheet's numbers: 6 groups of 6 digits. Only ever held in React state while it's
 * on screen; never put in a URL, storage, the clipboard or the console.
 */
export interface RecoverySheetData {
  sheet: string;
  created_at: string;
  groups: string[];
}
/** POST /api/auth/setup: the session plus the new vault's recovery sheet (older servers omit it). */
export interface SetupResponse extends SessionResponse {
  recovery?: RecoverySheetData;
}
export type RecoveryState = 'none' | 'active' | 'unconfirmed' | 'stale';
export interface RecoveryStatus {
  status: RecoveryState;
  sheet: string | null;
  created_at: string | null;
}
/** A new sheet made from Settings: it only replaces the old one after `activate`. */
export interface RecoveryPending extends RecoverySheetData {
  pending_id: string;
}
export type AuthErrorCode =
  | 'bad_format'
  | 'typo'
  | 'no_match'
  | 'outdated'
  | 'unusable'
  | 'no_recovery'
  | 'not_initialized'
  | 'rate_limited'
  | 'weak_password'
  | 'wrong_password'
  | 'password_required'
  | 'pending_expired'
  | 'finish_on_unlock'
  | 'backup_retry'
  | 'sheet_changed'
  | 'already_initialized'
  | 'bad_backup'
  | 'locked'
  | 'pre_migration_copy_failed';

export interface Account {
  id: number;
  source: 'plaid' | 'manual';
  item_id: number | null;
  name: string;
  official_name: string | null;
  mask: string | null;
  institution_name: string | null;
  category: Category;
  plaid_type: string | null;
  plaid_subtype: string | null;
  current_balance: number;
  available_balance: number | null;
  currency: string;
  interest_rate: number | null;
  minimum_payment: number | null;
  next_payment_due: string | null;
  notes: string | null;
  hidden: boolean;
  is_liability: boolean;
  updated_at: string;
  /** Release 3: effective loan group for liabilities (custom label or automatic); null for assets. */
  loan_group: string | null;
}

export interface NewManualAccount {
  name: string;
  category: Category;
  current_balance: number;
  institution_name?: string;
  interest_rate?: number;
  minimum_payment?: number;
  next_payment_due?: string;
  notes?: string;
}

export interface AccountPatch {
  name?: string;
  category?: Category;
  hidden?: boolean;
  notes?: string | null;
  interest_rate?: number | null;
  minimum_payment?: number | null;
  next_payment_due?: string | null;
  /** Manual accounts only (backend returns 400 for Plaid accounts). */
  current_balance?: number;
  /** Release 3: liabilities only; null resets to the automatic group. */
  loan_group?: string | null;
}

export interface BalancePoint {
  date: string;
  balance: number;
  /** Release 3: reconstructed from transactions, not a recorded balance. */
  estimated: boolean;
}

export interface Summary {
  net_worth: number;
  total_assets: number;
  total_liabilities: number;
  by_category: Record<Category, number>;
  account_count: number;
  last_synced_at: string | null;
  items_needing_attention: number;
  /** Release 2 */
  unread_alerts: number;
}

export interface NetWorthPoint {
  date: string;
  assets: number;
  liabilities: number;
  net_worth: number;
  /** Release 3: any component was estimated. */
  estimated: boolean;
}

export interface Transaction {
  id: number;
  account_id: number;
  account_name: string;
  date: string;
  name: string;
  merchant_name: string | null;
  /** Negative = money out, positive = money in. */
  amount: number;
  /** Effective category id (user override > rule > Plaid). Null means Other. */
  category: string | null;
  pending: boolean;
  /** Release 2 */
  category_name: string;
  category_hue: number;
  plaid_category: string | null;
  category_source: CategorySource;
  rule_id: number | null;
  notes: string | null;
  is_transfer: boolean;
  /** Release 3: when present, these lines replace the parent in every total. */
  splits: TransactionSplit[];
  tags: TagRef[];
  /** Release 3.3: plaid-set, not split, not a transfer, and not in a budget category (SPEC "Needs a category"). */
  needs_category: boolean;
  /** Release 3.3: up to 2 category ids used on past purchases from the same merchant (needs_category rows only). */
  suggested_categories: string[];
  /** Release 3.3: first enabled rule that matches this transaction (null for split rows). */
  matching_rule_id: number | null;
}

/**
 * Release 3.3: `user_bulk` = set by hand on several transactions at once (treated like `user`).
 * Release 3.6 phase 2: `auto` = sorted automatically into the budget's Groceries / Eating out /
 * Gas by Plaid's detailed category (still automatic: rules win; it isn't "Needs a category").
 */
export type CategorySource = 'plaid' | 'auto' | 'rule' | 'user' | 'user_bulk';
export type TxnView = 'all' | 'needs_category' | 'in' | 'out';

/** Money in / out on one day over the full filter set (transfers left out). money_out is ≤ 0. */
export interface DayTotals {
  money_in: number;
  money_out: number;
}

export interface TransactionPage {
  items: Transaction[];
  total: number;
  /** Release 3.3: keyset cursor for the next page; null at the end. */
  next_cursor: string | null;
  /** Release 3.3: one entry per date on this page. */
  days: Record<string, DayTotals>;
}

/** GET /api/transactions/summary. `counts` ignore `view`; everything else honors it. */
export interface TransactionSummary {
  total: number;
  first_date: string | null;
  money_in: number;
  money_out: number;
  needs_category: number;
  counts: Record<TxnView, number>;
}

/** A transaction's category state, for Undo. */
export interface CategoryState {
  id: number;
  category: string | null;
  category_source: CategorySource;
}

export interface BulkCategoryResult {
  /** Changed rows only. */
  items: Transaction[];
  previous: CategoryState[];
  /** Split transactions left as they were. */
  skipped: number[];
}

/** Every transaction from the same merchant (or name when there's no merchant). */
export interface TransactionRelated {
  field: 'merchant' | 'name';
  text: string;
  count: number;
  total: number;
  first_date: string;
}

/** Category contributions from the same ledger/accounts used by EnvelopeLine.spent. */
export interface BudgetCategoryTransaction {
  id: number;
  date: string;
  name: string;
  account_name: string;
  /** Negative purchase, positive refund; a split includes only this category's parts. */
  amount: number;
  pending: boolean;
  split: boolean;
}

export interface BudgetCategoryTransactionPage {
  items: BudgetCategoryTransaction[];
  total: number;
  next_offset: number | null;
  /** Full month net purchases, floored at zero just like EnvelopeLine.spent. */
  spent: number;
}

export interface TransactionQuery {
  account_id?: number;
  search?: string;
  start?: string;
  end?: string;
  limit?: number;
  offset?: number;
  /** Release 2: effective category id */
  category?: string;
  /** Release 3: tag id */
  tag?: number;
  /** Release 3.3 */
  view?: TxnView;
  /** Release 3.3: keyset cursor from `next_cursor` (don't combine with offset). */
  cursor?: string;
}

/** The filters shared by the list, summary, ids and CSV export. */
export type TransactionFilters = Omit<TransactionQuery, 'limit' | 'offset' | 'cursor'>;

export interface Holding {
  id: number;
  account_id: number;
  account_name: string;
  name: string;
  ticker: string | null;
  quantity: number;
  price: number | null;
  value: number;
  cost_basis: number | null;
}

export type PlaidEnv = 'sandbox' | 'production';
/** Where the active Plaid keys come from: the .env file, the vault (Settings → Bank connection), or nowhere. */
export type PlaidKeysSource = 'env' | 'vault' | 'none';
export type PlaidKeysTestCode = 'ok' | 'invalid_keys' | 'wrong_environment' | 'items_mismatch' | 'unreachable' | 'plaid_error';

export interface PlaidStatus {
  configured: boolean;
  env: PlaidEnv | string;
  source: PlaidKeysSource;
}

export interface PlaidKeysTest {
  ok: boolean;
  code: PlaidKeysTestCode;
  /** Server-owned plain-language message (never Plaid's own free text). */
  message: string;
  /** Plaid's error_code, sanitized; null when ok. */
  plaid_code: string | null;
  at: string;
  /** The environment that worked (or was tried, for a single-env test); null when an "auto" test found none. */
  env: PlaidEnv | null;
  /** Present on POST /test responses: true when a linked-institution probe ran. */
  checked_linked_items?: boolean;
}

export interface PlaidKeysStatus {
  source: PlaidKeysSource;
  client_id: string | null;
  env: PlaidEnv | null;
  /** Last 4 characters of the secret; the secret itself is never returned. */
  secret_hint: string | null;
  saved_at: string | null;
  last_test: PlaidKeysTest | null;
  /** The .env file has keys, so keys saved in the vault are ignored. */
  vault_keys_ignored: boolean;
  /** Exactly one of PLAID_CLIENT_ID / PLAID_SECRET is set in .env. */
  env_file_partial: boolean;
  /** Linked institutions (pending included). */
  linked_items: number;
}

export interface PlaidKeysInput {
  client_id: string;
  secret: string;
  env: PlaidEnv;
}
/** Candidate keys for POST /test; `auto` tries Production, then Sandbox (or only the linked banks' env). */
export type PlaidKeysTestInput = Omit<PlaidKeysInput, 'env'> & { env: PlaidEnv | 'auto' };

/** Error bodies from PUT/test carry a machine code alongside `detail`. */
export type PlaidKeysErrorCode = PlaidKeysTestCode | 'managed_by_env' | 'not_set' | 'items_changed';
export function plaidKeysErrorCode(e: unknown): PlaidKeysErrorCode | null {
  return apiErrorCode(e) as PlaidKeysErrorCode | null;
}
/** `plaid_code` from a PUT error body, if any. */
export function plaidKeysErrorPlaidCode(e: unknown): string | null {
  if (!(e instanceof ApiError) || !e.body || typeof e.body !== 'object' || !('plaid_code' in e.body)) return null;
  const c = (e.body as { plaid_code: unknown }).plaid_code;
  return typeof c === 'string' ? c : null;
}

export interface PlaidItem {
  id: number;
  institution_name: string | null;
  kind: ItemKind;
  status: ItemStatus;
  error_code: string | null;
  last_synced_at: string | null;
  account_count: number;
}

export interface SyncItemResult {
  item_id: number;
  institution_name: string | null;
  ok: boolean;
  error_code: string | null;
  accounts: number;
  transactions_added: number;
  transactions_modified: number;
  transactions_removed: number;
  holdings: number;
}

export interface SyncResponse {
  results: SyncItemResult[];
}

// ---------------------------------------------------------------- Release 2 models
// Contract: SPEC.md "Release 2". Months are 'YYYY-MM'; money is dollars.

export type CategoryKind = 'spending' | 'income' | 'transfer' | 'fixed';

/** Transaction category (Plaid primaries seeded, plus custom `c_<n>`). Not the account `Category`. */
export interface TxnCategory {
  id: string;
  name: string;
  hue: number;
  kind: CategoryKind;
  custom: boolean;
  hidden: boolean;
  /** Release 3 */
  group_id: number | null;
  target: CategoryTarget | null;
}

export interface TransactionPatch {
  /** null resets to automatic (rules, then Plaid). */
  category?: string | null;
  notes?: string | null;
  is_transfer?: boolean;
}

export interface DiscoveredAccount {
  plaid_account_id: string;
  name: string;
  official_name: string | null;
  mask: string | null;
  category: Category;
  plaid_type: string | null;
  plaid_subtype: string | null;
  current_balance: number;
  is_liability: boolean;
  /** Already imported as a local account. */
  imported: boolean;
}

export interface ExchangeResponse {
  item: PlaidItem;
  accounts: DiscoveredAccount[];
}

export interface ImportResponse {
  item: PlaidItem;
  result: SyncItemResult;
}

// budgets / spending
// Envelope budgets (Release 2.1, SPEC.md "Envelope budgets"). Every category is an
// envelope: available = carryover + assigned − spent. "Ready to assign" is real money
// in the budget accounts minus everything already assigned, so you only budget cash you have.

export interface EnvelopeLine {
  category: string;
  name: string;
  hue: number;
  /** Positive leftover rolled in from last month (overspending does not carry). */
  carryover: number;
  /** The month's full amount: the plan plus this month's one-time moves (`moved`). */
  assigned: number;
  /**
   * Release 3.17: this month's one-time moves into (+) or out of (−) the category, already
   * included in `assigned`. The plan (what repeats next month) = assigned − moved.
   */
  moved: number;
  /** Spending on budget accounts this month (refunds net out). */
  spent: number;
  /** carryover + assigned − spent. Negative = overspent. */
  available: number;
  /** Release 3 */
  group_id: number | null;
  /** A category the user made (only those can be deleted; the app's own ones can only leave the plan). */
  custom?: boolean;
  /** `needed` = what to assign this month to stay on track (0 if on track). */
  target: (CategoryTarget & { needed: number }) | null;
  /**
   * Phase 2: this month's recurring bills in the category (the calendar's occurrences: moves
   * and skips applied, paid at the actual amount, card bills included). Null without bills,
   * and for months that ended more than 62 days ago (`BudgetMonth.bills_available` false).
   */
  bills: CategoryBills | null;
  /**
   * Release 3.8: the goal this category belongs to (Goals page), or null. `plan`: what the
   * goal's rule plans for this month (min(monthly, target − saved); 0 once reached), which the
   * quick fills use instead of last month's numbers.
   */
  goal: { id: number; kind: GoalKind; saved: number; target: number; reached: boolean; plan: number } | null;
  /** Release 3.8: a "money you already had" row in the month before (setup's or a goal's Already saved), not a plan. */
  seeded: boolean;
}

/** A bill occurrence's status (same as the calendar's). `past`: an old one never tracked. */
export type BillStatus = 'upcoming' | 'pending' | 'late' | 'paid' | 'skipped' | 'past';

export interface CategoryBill {
  /** The calendar occurrence key (`r{id}:{base date}`). */
  key: string;
  recurring_id: number;
  name: string;
  /** Effective due date (after a move). */
  date: string;
  /** Expected amount, positive. */
  amount: number;
  status: BillStatus;
  /** What was paid (positive), when paid. */
  actual_amount: number | null;
  /** When it was paid (the matched transaction's date), when paid. */
  paid_date: string | null;
}

export interface CategoryBills {
  /** Everything but skipped; paid ones at the actual amount. */
  total: number;
  items: CategoryBill[];
}

export interface SpendLine {
  category: string;
  name: string;
  hue: number;
  spent: number;
}

export interface BudgetAccount {
  id: number;
  name: string;
  mask: string | null;
  institution_name: string | null;
  category: Category;
  /** Signed: cash positive, credit-card debt negative. */
  balance: number;
  included: boolean;
}

export interface BudgetMonth {
  month: string;
  is_current: boolean;
  is_future: boolean;
  day_of_month: number;
  days_in_month: number;
  days_left: number;
  /** day_of_month / days_in_month for the current month; 1 for past; 0 for future months. */
  pace: number;
  /** Global, as of today (same value on every month's view). Negative = over-assigned. */
  ready_to_assign: number;
  /** The part of ready_to_assign that is income still expected this month (same on every month's view). */
  ready_pending: number;
  /** Signed sum of the included budget accounts. */
  cash_total: number;
  /** Every account that could fund the budget (bank, credit, other), with `included`. */
  budget_accounts: BudgetAccount[];
  /** Totals for this month over `categories`. */
  assigned: number;
  spent: number;
  available: number;
  categories: EnvelopeLine[];
  /** Spending on budget accounts this month in categories not in the budget. */
  unbudgeted: SpendLine[];
  fixed: SpendLine[];
  /** Spending categories not in this month's budget (for "Add a category"). */
  addable: { id: string; name: string; hue: number }[];
  /** Setup's "money you already had" row (Emergency savings in the month before setup): not a plan. */
  seed_category: string | null;
  earliest_month: string | null;
  /** Current month + 12. */
  latest_month: string;
  /** Release 3 */
  groups: { id: number; name: string; position: number }[];
  /** Σ target `needed` over this month's categories. */
  needed_total: number;
  /** Release 3.6 (Budget for a beginner): "Money for {Month}" = ready_to_assign + assigned. */
  month_money: number;
  income: BudgetIncome;
  /** The Emergency savings category (a setting, never found by name); null when unset. */
  savings_category: string | null;
  /** No budget rows at all and the guided setup never ran: show the setup card. */
  setup_needed: boolean;
  /** Phase 2: false for months that ended more than 62 days ago (no bills per category there). */
  bills_available: boolean;
  /** Phase 2: visible spending categories with bills this month that aren't in the plan. */
  bills_outside: { category: string; name: string; hue: number; total: number }[];
}

// ---------------------------------------------------------------- Release 3.6: Budget for a beginner
// Contract: SPEC.md "Release 3.6".

/** "expected": paychecks still expected this month count (pending); "off": only money already in. */
export type BudgetIncomeMode = 'expected' | 'off';

export type IncomeEvent =
  | { kind: 'more'; amount: number; date: string | null; name: string | null; expected: number; received: number }
  | {
      kind: 'less';
      amount: number;
      date: string | null;
      name: string | null;
      expected: number;
      received: number;
      reason: 'short' | 'missing';
      can_use_unplanned: boolean;
    };

export interface BudgetIncome {
  mode: BudgetIncomeMode;
  /** expected_override ?? suggested. */
  expected: number | null;
  expected_override: number | null;
  /** Average of the last 3 complete months with income, rounded down to $10. */
  suggested: number | null;
  /** Income-kind deposits on the budget accounts in the viewed month (pending included). */
  received: number;
  /** max(0, expected − received), only for today's month in "expected" mode; part of Ready to assign. */
  pending: number;
  next: { date: string; name: string; amount: number } | null;
  event: IncomeEvent | null;
  /** Release 3.7: "paychecks" = this month's paychecks (at least one active income item), else the 3-month average. */
  suggested_source?: 'paychecks' | 'average';
}

export type BudgetSetupKey = 'groceries' | 'gas' | 'eating_out' | 'bills' | 'fun' | 'other';

export interface BudgetSetupRow {
  key: BudgetSetupKey;
  label: string;
  hint: string;
  /** The category names this row fills. */
  categories: string[];
  /** Average a month over the last 3 complete months, or null without history. */
  average: number | null;
  chips: number[];
  suggested: number;
}

export interface BudgetSetupInfo {
  month: string;
  needed: boolean;
  income: {
    /** The last 3 complete months' average (null without income). */
    suggested: number | null;
    received: number;
    /** Paychecks on the calendar still expected this month. */
    expected_more: number;
    /** The amount the screen starts from: received + still expected when paychecks are on the calendar, else the larger of the average and what came in. */
    start: number | null;
  };
  rows: BudgetSetupRow[];
  /** This month's recurring bills with a budget category (calendar occurrences); null without any. */
  bills: {
    total: number;
    count: number;
    by_category: Record<string, number>;
    /** The usual other spending of categories that joined the Bills row for a bill (in the row's suggestion). */
    extra: number;
    extra_categories: string[];
  } | null;
  /** Money there before this month's income; kept in Emergency savings unless declined. */
  existing_money: number;
  /** How much more the cards owe than the accounts hold: it comes out of this month's income. */
  owed_beyond_cash: number;
}

export interface BudgetSetupInput {
  answers: Partial<Record<BudgetSetupKey, number>>;
  income_expected: number;
  keep_existing_in_savings: boolean;
  skip?: boolean;
}

export interface BudgetSettingsInput {
  income_mode?: BudgetIncomeMode;
  savings_category?: string | null;
}

/** Release 3.14: one category's `budgets` row in a month, exactly (null = no row). For an exact Undo. */
export interface BudgetRowState {
  assigned: number;
  /** Release 3.17: the row's one-time moves (part of `assigned`). PUT: missing = 0. */
  moved: number;
  removed: boolean;
  restart: boolean;
}

/** Release 3.14: one row for `api.budgets.putBackRows`. */
export interface BudgetRowRestore {
  category: string;
  state: BudgetRowState | null;
  expected: BudgetRowState | null;
}

export interface BudgetSave {
  /** Partial update: only listed categories change. Adds a category to the budget if absent. */
  assigned?: Record<string, number>;
  /**
   * Release 3.17: the month's one-time moves per category, ABSOLUTE (Move money, Cover it). Every
   * key must also be in `assigned`. A category in `assigned` without an entry here gets 0: the
   * amount becomes its plan (which repeats in later months).
   */
  moved?: Record<string, number>;
  /** Remove these categories from the budget from this month on. */
  removed?: string[];
  /** Release 3.6: this month's expected income (null = the suggestion). Today's month, "expected" mode only. */
  income_expected?: number | null;
  /** "Leave it for next month": income_expected must equal what came in; skips the over-plan check once. */
  accept_received?: boolean;
  /** Undo putting back an earlier state: income_expected may be below what came in (every other rule applies). */
  restore?: boolean;
}

export interface SpendingReview {
  month: string;
  income: number;
  fixed: number;
  budgeted_spending: number;
  other_spending: number;
  left_over: number;
  left_over_pct: number | null;
  compare: { prev_month: string; prev_spent: number; spent: number; delta_pct: number | null };
  biggest: { category: string; name: string; hue: number; spent: number } | null;
  most_visited: { merchant: string; count: number; spent: number } | null;
  history: { month: string; spent: number; assigned: number }[];
}

// recurring / forecast
/** Release 3.4: `once` is manual-only (detection never produces it). */
export type Cadence = 'once' | 'weekly' | 'biweekly' | 'semimonthly' | 'monthly' | 'quarterly' | 'yearly';
export type RecurringStatus = 'suggested' | 'active' | 'dismissed';
/** "Remind me": off, 1 day before, 3 days before (in-app reminders only). */
export type ReminderDays = 0 | 1 | 3;

export interface RecurringItem {
  id: number;
  name: string;
  account_id: number | null;
  account_name: string | null;
  /** Signed: + money in, − money out. */
  amount: number;
  cadence: Cadence;
  next_date: string;
  status: RecurringStatus;
  include_in_forecast: boolean;
  source: 'detected' | 'manual';
  last_seen_date: string | null;
  /** Release 3.4 (calendar) */
  reminder_days: ReminderDays;
  /** No occurrences before this date (null = no limit). Set to next_date on create. */
  start_date: string | null;
  /** Read-only: the parsed anchor day(s) of the month (31 = last day). */
  anchor_days: number[] | null;
  /** Budget category this bill belongs to ("one bill, two places"). */
  category_id: string | null;
  category_name: string | null;
  /**
   * Release 3.19, only in GET /api/recurring: "Netflix now charges $17.99. Update your amount?"
   * for a bill whose amount the user set by hand, until they answer (null: nothing to ask, or the
   * Settings "price" switch is off). Yes = `update(id, { amount })`; No = `priceAnswer`.
   */
  price_question?: PriceQuestion | null;
}

/** A new charge at a different price for a bill whose amount the user set by hand (Release 3.19). */
export interface PriceQuestion {
  transaction_id: number;
  /** Signed, like the item's amount (− money out). */
  amount: number;
  /** The charge's date (YYYY-MM-DD). */
  date: string;
}

export interface RecurringInput {
  name: string;
  amount: number;
  cadence: Cadence;
  next_date: string;
  account_id?: number | null;
  reminder_days?: ReminderDays;
  category_id?: string | null;
}

export type RecurringPatch = Partial<RecurringInput> & {
  status?: RecurringStatus;
  include_in_forecast?: boolean;
};

// Release 3.4: cash forecast calendar (MAPPING §5, BILLS-ADDENDUM)

export interface OccurrenceOverride {
  base_date: string;
  moved_to: string | null;
  skipped: boolean;
  paid: boolean;
  paid_amount: number | null;
}
export interface OccurrenceOverrideInput {
  moved_to?: string | null;
  skipped?: boolean;
  paid?: boolean;
  paid_amount?: number | null;
}
export interface OccurrenceChange {
  override: OccurrenceOverride | null;
  previous: OccurrenceOverride | null;
}

export type OccurrenceKind = 'in' | 'out' | 'card' | 'plan';
export type OccurrenceStatus = 'upcoming' | 'pending' | 'late' | 'paid' | 'past' | 'skipped';

export interface ForecastOccurrence {
  /** Opaque: "r12:2026-10-08" | "p:TRAVEL:2026-12-12". */
  key: string;
  /** "r12" | "p:TRAVEL" (matches ForecastSeries.id). */
  series: string;
  recurring_id: number | null;
  plan_category: string | null;
  base_date: string;
  /** Effective date (after a move). */
  date: string;
  name: string;
  /** Expected, signed. */
  amount: number;
  kind: OccurrenceKind;
  status: OccurrenceStatus;
  counted: boolean;
  counted_on: string | null;
  /** A late/pending/today item counted on tomorrow ("still expected"). */
  carried: boolean;
  actual: { amount: number; date: string | null; transaction_id: number | null; by: 'match' | 'you' } | null;
  override: OccurrenceOverride | null;
  /** Recurring, not paid/skipped/past. */
  movable: boolean;
  /** Eligible for "Move all future ones too". */
  next_in_series: boolean;
  reminder_days: ReminderDays;
  category_id: string | null;
  /**
   * Release 3.19: true only for rows of `ForecastCalendar.maybe` (a suggested item, never
   * counted); false for every row of `occurrences`.
   */
  maybe: boolean;
}

export interface ForecastDay {
  date: string;
  /** null before today; today = the current balance. */
  balance: number | null;
  below: boolean;
}
export interface ForecastDip {
  date: string;
  balance: number;
  cause_key: string | null;
}
export interface ForecastMonth {
  /** YYYY-MM */
  month: string;
  low: number;
  low_date: string;
  /** null when the month ends after `to`. */
  end: number | null;
  end_date: string;
  first_dip: ForecastDip | null;
}
export interface ForecastSeries {
  id: string;
  recurring_id: number | null;
  plan_category: string | null;
  name: string;
  amount: number;
  cadence: Cadence;
  anchor_days: number[] | null;
  next_date: string | null;
  kind: OccurrenceKind;
  source: 'detected' | 'manual' | 'spending';
  account: { id: number; name: string; mask: string | null; category: Category } | null;
  counted: boolean;
  reminder_days: ReminderDays;
  can_move_all: boolean;
  category_id: string | null;
}
export interface ForecastCalendar {
  account: { id: number; name: string; mask: string | null; institution_name: string | null } | null;
  today: string;
  from: string;
  to: string;
  /** Last day of month(today) + 12. */
  horizon_end: string;
  balance: number;
  threshold: number;
  daily_spend: number;
  include_daily: boolean;
  low_ahead: { enabled: boolean; days: number };
  reminders_enabled: boolean;
  days: ForecastDay[];
  occurrences: ForecastOccurrence[];
  months: ForecastMonth[];
  first_dip: ForecastDip | null;
  series: ForecastSeries[];
  /**
   * Release 3.19: "Maybe" bills, upcoming dates of suggested items (from today, within
   * [from, to]). Display only: never in days, months, first_dip, series or `occurrences`, so
   * nothing that totals or projects reads them. Yes = `recurring.update(id, {status: 'active'})`,
   * No = `{status: 'dismissed'}`.
   */
  maybe: ForecastOccurrence[];
}
export interface ForecastSettings {
  include_daily: boolean;
  excluded_plans: string[];
}
export type ForecastSettingsPatch = Partial<ForecastSettings>;

export interface RecurringCandidate {
  item: RecurringItem;
  account: { id: number; name: string; mask: string | null; category: Category } | null;
  paid_with: 'checking' | 'card' | 'other';
  on_calendar: boolean;
  /** Monthly/quarterly/yearly anchor; 31 = last day. */
  day_of_month: number | null;
  /** Weekly/biweekly, 0 = Sunday. */
  weekday: number | null;
  /** YYYY-MM, oldest first, at most 4. */
  months_seen: string[];
  count: number;
  last_date: string | null;
}
export interface RecurringSnapshot {
  item: RecurringItem & { merchant_key: string; created_at: string };
  overrides: OccurrenceOverride[];
}
export interface RescheduleResult {
  item: RecurringItem;
  undo: RecurringSnapshot;
}

export interface ForecastEvent {
  date: string;
  recurring_id: number;
  name: string;
  amount: number;
  kind: 'in' | 'out' | 'card';
  included: boolean;
}

export interface Forecast {
  account: { id: number; name: string; mask: string | null; institution_name: string | null } | null;
  balance: number;
  today: string;
  end: string;
  daily_spend: number;
  /** Low-balance alert value. */
  threshold: number;
  events: ForecastEvent[];
}

// goals (Release 3.8, SPEC "Goals (D8) and Investments (D9) redesign"). Each goal is a Budget
// category; money in dollars, months 'YYYY-MM'. Debt goals are no longer returned.
export type GoalKind = 'emergency' | 'save';
export type GoalStatus = 'on_track' | 'behind' | 'reached' | 'no_plan';

export interface Goal {
  id: number;
  kind: GoalKind;
  name: string;
  /** The linked Budget category (null for an old goal not linked yet). */
  category_id: string | null;
  /** false: an old goal ("Not in your Budget yet"); saving it once in Edit links it. */
  in_budget: boolean;
  target: number;
  /** 'YYYY-MM' (save goals); null for the emergency fund. */
  due_month: string | null;
  monthly: number;
  /** This month's plan: min(monthly, max(0, target − saved)). */
  planned_this_month: number;
  /**
   * The lowest this month's plan goes when the monthly amount or target is lowered (only this
   * month's own plan moves out, never more than the envelope still has); null when not in the Budget.
   */
  lowest_plan: number | null;
  /** The envelope, not counting this month's plan. */
  saved: number;
  /** Only the "Keep the money in" label. */
  account_id: number | null;
  /** "Savings ··2210" */
  account_label: string | null;
  status: GoalStatus;
  /** due − this month + 1 (save goals). */
  months_to_save: number | null;
  /** saved + monthly × months_to_save, capped at target. */
  projected: number;
  /** Emergency fund: the month it reaches the target (null when monthly is 0). */
  projected_month: string | null;
  /** target − projected, when behind. */
  short_by: number | null;
  /** ceil((target − saved) / months_to_save), rounded up to $5. */
  needed_monthly: number | null;
}

export interface GoalsState {
  month: string;
  /** Budget's "Money for {Month}". */
  month_money: number;
  /** Budget's "Not planned yet" (this month). */
  not_planned: number;
  /** Σ this month's goal plans. */
  monthly_total: number;
  /** false: the Budget isn't set up yet ("Set up your Budget first →"). */
  budget_ready: boolean;
  /** 3 × this month's bills (rounded up to $100), or null. */
  emergency_suggestion: number | null;
  /** What's already in Emergency savings while no emergency goal exists (it becomes the goal's). */
  emergency_available: number | null;
  /** What the suggestion is based on: this month's bills, or spending + fixed costs. */
  emergency_basis: 'bills' | 'spending' | null;
  /** The monthly figure before × 3 (null without a suggestion). */
  emergency_monthly: number | null;
  goals: Goal[];
}

export interface GoalInput {
  kind: GoalKind;
  name: string;
  target: number;
  /** Save goals: this month + 1 … + 24. Emergency fund: null. */
  due_month: string | null;
  monthly: number;
  /** Comes out of Not planned yet, then the emergency fund. New goals only. */
  already_saved: number;
  account_id?: number | null;
}

/**
 * `kind` can't change. PATCHing an old unlinked goal links it; only that PATCH may carry
 * `already_saved` (422 otherwise).
 */
export type GoalPatch = Partial<Pick<GoalInput, 'name' | 'target' | 'due_month' | 'monthly' | 'account_id' | 'already_saved'>> & {
  /** Only the page's Undo of an edit: this month's plan as it was, put back exactly (409 once the month changed). */
  plan_before?: { month: string; planned: number };
};

/** DELETE's `undo`: POST it back to /api/goals/restore unchanged (the server keeps its own record, by `token`). */
export interface GoalUndo {
  goal: { kind: GoalKind; name: string; target: number; due_month: string | null; monthly: number; account_id: number | null };
  category_id: string | null;
  month: string;
  assigned: number;
  /** What went back to Not planned yet (negative: overspending that came out of it). 0 when nothing left the Budget. */
  released: number;
  seed_month: string | null;
  /** The delete took the goal's category out of this month's Budget. */
  removed: boolean;
  token: string;
}

export interface GoalDeleted {
  state: GoalsState;
  undo: GoalUndo;
  /** The category the goal had taken over (Emergency savings), now back as it was with its money; null otherwise. */
  kept_in: string | null;
}

/** DELETE ?reason=undo_add (the page's Undo right after adding a goal); there is no Undo of it. */
export interface GoalAddUndone {
  state: GoalsState;
  /** The taken-over category (Emergency savings), back exactly as before the add; null when the goal made its own. */
  kept_in: string | null;
  /** What went back to Not planned yet (negative: overspending that came out of it). */
  released: number;
}

// investments (Release 3.8)
export type MixKind = 'us_stock' | 'intl_stock' | 'bond' | 'target_date' | 'cash' | 'company_stock' | 'other' | 'not_broken_down';

export interface InvestmentMix {
  kind: MixKind;
  /** Retirement date funds: the year in the fund's name. */
  year: number | null;
  value: number;
  /** Percent of the account (or of the total for the top-level mix), 0–100. */
  pct: number;
  /** The real holding names in this group. */
  names: string[];
}

export interface InvestmentPoint {
  month: string;
  worth: number;
  /** No snapshot that month: the previous value carried forward. */
  carried: boolean;
  /** Total history only: accounts that start this month. */
  added: string[];
}

export interface InvestmentAccount {
  id: number;
  name: string;
  institution_name: string | null;
  category: 'retirement' | 'hsa' | 'investment';
  source: 'plaid' | 'manual';
  worth: number;
  /** vs the last snapshot on or before the end of last month; null: "New this month". */
  change: number | null;
  change_pct: number | null;
  /** 'YYYY-MM': the first month with a snapshot. */
  tracked_since: string | null;
  /** worth − the account's first real snapshot. */
  change_since_tracking: number | null;
  updated_at: string | null;
  connection: { item_id: number; status: ItemStatus; kind: ItemKind; last_synced_at: string | null } | null;
  /** false: the bank shares no holdings ("Your bank doesn't say what this holds"). */
  holdings_known: boolean;
  /** The holdings add up to more than the balance (a stale balance): the mix shows what it holds. */
  holdings_differ: boolean;
  mix: InvestmentMix[];
}

export interface Investments {
  /** ISO datetime: the newest updated_at of the listed accounts (null without any). */
  as_of: string | null;
  last_month: string;
  total: {
    worth: number;
    change: number | null;
    change_pct: number | null;
    /** Accounts left out of the total change (no value at the end of last month). */
    change_missing: string[];
    /** The earliest account's first month. */
    tracked_since: string | null;
    /** Σ of the accounts' change_since_tracking (an account joining later isn't growth). */
    change_since_tracking: number | null;
  };
  accounts: InvestmentAccount[];
  mix: InvestmentMix[];
  /** Any account's holdings add up to more than its balance. */
  holdings_differ: boolean;
  history: { total: InvestmentPoint[]; by_account: Record<string, InvestmentPoint[]> };
}

// rules
export type RuleField = 'any' | 'merchant' | 'name';
export type RuleOp = 'contains' | 'is';

export interface RuleInput {
  field: RuleField;
  op: RuleOp;
  text: string;
  amount_op?: 'gt' | 'lt' | null;
  /** Compared with |transaction amount|. */
  amount?: number | null;
  action: 'category' | 'transfer';
  category?: string | null;
  enabled?: boolean;
  /** Create only: put the new rule ahead of every existing one (default: last). */
  first?: boolean;
}

export interface Rule extends Required<Omit<RuleInput, 'amount_op' | 'amount' | 'category' | 'first'>> {
  id: number;
  position: number;
  amount_op: 'gt' | 'lt' | null;
  amount: number | null;
  category: string | null;
  matches: number;
}

// alerts
export type AlertKey = 'low' | 'big' | 'budget' | 'due' | 'newrec' | 'price' | 'conn' | 'income' | 'low_ahead' | 'reminder';

export interface AlertSetting {
  key: AlertKey;
  enabled: boolean;
  value: number | null;
  unit: 'usd' | 'percent' | 'days' | null;
  last: { date: string; text: string } | null;
}

export interface PreviousVault {
  /** Folder name, e.g. `pre-restore-20260927-101500`. */
  id: string;
  /** Local time the copy was set aside (no timezone). */
  created_at: string;
  size_bytes: number;
}

export interface AlertEvent {
  id: number;
  key: AlertKey;
  severity: 'warn' | 'neg' | 'accent';
  title: string;
  body: string;
  created_at: string;
  read: boolean;
}

// ---------------------------------------------------------------- Release 3 models
// Contract: SPEC.md "Release 3".

export type CategoryTarget =
  | {
      kind: 'monthly' | 'by_date';
      amount: number;
      /** by_date only (YYYY-MM-DD). */
      date: string | null;
    }
  | {
      /**
       * Release 3.6 phase 2 "Cover my bills": no stored amount. In a budget month `amount` is
       * that month's bill total; in /api/categories it's null.
       */
      kind: 'bills';
      amount: number | null;
      date: null;
    };

export interface CategoryPatch {
  name?: string;
  hue?: number;
  kind?: CategoryKind;
  hidden?: boolean;
  group_id?: number | null;
  /** null clears the target. */
  target?: { kind: 'monthly'; amount: number } | { kind: 'by_date'; amount: number; date: string } | { kind: 'bills' } | null;
  /** Undo putting back an earlier `target`: a by-date target's date may have passed. */
  restore?: boolean;
}

export interface CategoryGroup {
  id: number;
  name: string;
  position: number;
  category_ids: string[];
}

export interface FundTargetsResult {
  month: BudgetMonth;
  funded: number;
  unfunded: number;
}

export interface TransactionSplit {
  id: number;
  amount: number;
  category: string;
  category_name: string;
  category_hue: number;
  notes: string | null;
}

export interface SplitInput {
  amount: number;
  category: string;
  notes?: string | null;
}

export interface TagRef {
  id: number;
  name: string;
  hue: number;
}

export interface Tag extends TagRef {
  count: number;
  /** All-time spending (split-aware). */
  spent: number;
}

export interface RecategorizeMatch {
  field: 'merchant' | 'name';
  text: string;
  category: string;
}

export interface RecategorizePreview {
  /** Transactions that would change. */
  matches: number;
  /** Already in that category. */
  already: number;
  /** Among `matches`, set by hand before (skipped unless include_manual). */
  manual: number;
}

export interface MonthlyReportMonth {
  month: string;
  income: number;
  spending: number;
  fixed: number;
  net: number;
  savings_rate: number | null;
  by_category: Record<string, number>;
  /** Keys are group ids as strings, or "none". */
  by_group: Record<string, number>;
  /** Fixed-kind categories (loan payments), which `spending` and `by_category` leave out. */
  by_fixed: Record<string, number>;
  /** Release 3.14: the month's budget plan (dollars) per spending or fixed category that has one;
   *  `{}` before budgets exist. A range's plan is the sum of its months' plans. */
  planned: Record<string, number>;
}

export interface MonthlyReportCategory {
  id: string;
  name: string;
  hue: number;
  kind: 'spending' | 'fixed';
  group_id: number | null;
  /** A fixed bill: fixed kind, or at least 75% of its last 6 months came from recurring payments. */
  bill: boolean;
}

export interface MonthlyReport {
  months: MonthlyReportMonth[];
  /** Spending categories in display order, then fixed categories. */
  categories: MonthlyReportCategory[];
  groups: { id: number; name: string }[];
  /** Month of the first transaction on the spending accounts, however far back; null without any. */
  first_month: string | null;
}

/** GET /api/reports/category/{id}?start&end&months */
export interface CategoryReport {
  category: string;
  /** `months` months (default 6) ending with `end`'s month, oldest first. */
  months: { month: string; spent: number }[];
  /** Top 5 merchants between start and end, largest first. `count` = transactions. */
  merchants: { name: string; spent: number; count: number }[];
  /** The 5 largest purchases between start and end (split parts count on their own). */
  biggest: { id: number; date: string; name: string; amount: number }[];
}

/** GET /api/reports/habits?month&under — day-to-day spending only (bills left out). */
export interface HabitsReport {
  month: string;
  /** Last day covered (today for the current month); null for a future month. */
  through: string | null;
  /** Every day from the 1st through `through`. `spent` never goes below 0. `places` (Release 3.14):
   *  up to 2 store names that day, most spent first (money out only). */
  days: { date: string; spent: number; places?: string[] }[];
  small: {
    under: number;
    count: number;
    total: number;
    /** Top 5 by total. */
    merchants: { name: string; count: number; total: number }[];
  };
}

/** Release 3.14: `GET /api/reports/yoy?mode=year|month` (Release 3.18: `&month=YYYY-MM` in month mode). */
export type YoyMode = 'year' | 'month';

/** One side of the Year over year comparison (spending categories, bills included). */
export interface YoySide {
  /** ISO dates, inclusive. */
  start: string;
  end: string;
  total: number;
  /** `year`: Jan → this month (last month cut at the same day); `month`: one entry. Oldest first.
   *  `month` is this side's own "YYYY-MM" (previous side = last year's months), so pair by index. */
  months: { month: string; total: number }[];
  /** Largest first; categories with nothing spent are left out. `bill` = the inferred bill flag (as of today). */
  categories: { id: string; name: string; hue: number; bill: boolean; total: number }[];
  /** Grouped like "Where, exactly". `category` = the category id it spent most in; `count` = visits.
   *  Largest first; stores with nothing spent (net) are left out. */
  merchants: { name: string; category: string; total: number; count: number }[];
}

export interface YoyReport {
  mode: YoyMode;
  /** Month mode: the month compared ("YYYY-MM"); null in year mode. */
  month: string | null;
  /** True when the range is cut at today ("so far"); false for a finished month (whole months). */
  partial: boolean;
  current: YoySide;
  previous: YoySide;
  /** The first month with any data ("YYYY-MM"), null with no data at all. */
  first_month: string | null;
}

/** Release 3.14: `GET /api/reports/subscriptions`: recurring money-out items charged to a credit card
 *  (not loans, the debt category, card bill payments, transfers, one-time or dismissed items). */
export interface SubscriptionItem {
  /** The Recurring item's id. */
  id: number;
  name: string;
  /** The item's category id (else the one its charges use most); null = none. */
  category: string | null;
  /** The credit card it's charged to (`accounts.name`). Only items on a card are listed; always set. */
  card: { id: number; name: string } | null;
  cadence: Cadence;
  /** The charge converted to a month (dollars, positive). */
  monthly: number;
  /** One charge (dollars, positive = money out). */
  amount: number;
  /** The most recent price change in the last 12 months; `amount` is signed dollars (positive =
   *  went up), `month` is "YYYY-MM". null = no change found (or no history). */
  change: { amount: number; month: string } | null;
  /** Charges were seen all year (first matched charge 11+ months ago): with `change` null, the UI
   *  may say "Same price as last year"; when false it says nothing. */
  full_year: boolean;
  /** Release 3.15: the next charge still expected (calendar moves and skips applied), "YYYY-MM-DD";
   *  null when none is coming. */
  next_date: string | null;
}

export interface SubscriptionsReport {
  /** Largest `monthly` first. */
  items: SubscriptionItem[];
  monthly_total: number;
}

export interface PayoffSummary {
  months: number | null;
  payoff_date: string | null;
  total_interest: number;
  never: boolean;
}

export interface PayoffProjection extends PayoffSummary {
  account_id: number;
  name: string;
  balance: number;
  apr: number;
  minimum: number;
  extra: number;
  /** Starts next month. */
  schedule: { month: string; balance: number; interest: number; principal: number }[];
  /** Same projection with extra = 0. */
  baseline: PayoffSummary;
  history: BalancePoint[];
}

export type DebtStrategy = 'avalanche' | 'snowball' | 'custom';

export interface DebtPlanInput {
  extra: number;
  strategy: DebtStrategy;
  /** custom only: account ids, first paid first. */
  order?: number[];
  account_ids?: number[];
}

export interface DebtPlan extends PayoffSummary {
  strategy: DebtStrategy;
  extra: number;
  debts: {
    account_id: number;
    name: string;
    loan_group: string | null;
    balance: number;
    apr: number;
    minimum: number;
    payoff_months: number | null;
    payoff_date: string | null;
    interest: number;
  }[];
  timeline: { month: string; total: number; by_account: Record<string, number> }[];
  compare: Record<'avalanche' | 'snowball' | 'minimums_only', { months: number | null; total_interest: number }>;
}

export interface AutoBackup {
  dir: string | null;
  keep: number;
  last_at: string | null;
  last_error: string | null;
  /** Home "Today": when the last failure happened and what kind it was (null once a backup works). */
  last_error_at?: string | null;
  last_error_code?: BackupErrorCode | null;
  files: { name: string; size_bytes: number; created_at: string }[];
}

// ---------------------------------------------------------------- Home v2 (GET /api/dashboard)
// Contract: SPEC.md "Home v2 (Dashboard v2 handoff, Release 3.9)".

export type HomeTone = 'act' | 'warn' | 'info';
export type BackupErrorCode = 'missing' | 'denied' | 'write' | 'network' | 'other';

interface NeedBase<K extends string, T extends HomeTone, D> {
  key: string;
  kind: K;
  tone: T;
  dismissible: boolean;
  /** "Not now" hides the need until this changes. */
  fingerprint: string;
  data: D;
}

export interface OverPlanOne {
  category_id: string;
  name: string;
  planned: number;
  spent: number;
  over: number;
  month: string;
}
/** key `over_plan:many` (more than 3 categories). Only categories with something planned count. */
export interface OverPlanMany {
  count: number;
  month: string;
  total_over: number;
  /** The first 3. */
  names: string[];
}
export interface NewRecurringOne {
  recurring_id: number;
  name: string;
  amount: number;
  /** Charges only. */
  kind: 'out';
  cadence: Cadence | null;
  account_name: string | null;
}
/** key `newrec:many` (more than 3 new ones). */
export interface NewRecurringMany {
  count: number;
  /** The first 3. */
  names: string[];
}

/**
 * One row of "Things that need you". The server builds every kind but `recovery_sheet`
 * (built on the client from GET /api/recovery) and leaves out kinds whose Settings switch is
 * off. `over_plan` and `new_recurring` become one ":many" row when there are more than 3.
 * The server does not drop dismissed ones: the client filters with `dismissed`.
 */
export type HomeAlert =
  | NeedBase<'bank_signin' | 'bank_error', HomeTone, { item_id: number; item_kind: ItemKind; institution_name: string | null; error_code?: string | null }>
  | NeedBase<'finish_setup', 'info', { item_id: number; institution_name: string | null }>
  | NeedBase<'low_balance', 'warn', { account_id: number; account_name: string | null; date: string; balance: number; threshold: number; cause: string | null }>
  | NeedBase<'over_plan', 'warn', OverPlanOne | OverPlanMany>
  | NeedBase<'backup_failed', 'warn', { code: BackupErrorCode; folder_name: string | null; at: string }>
  | NeedBase<'bill_due', 'info', { recurring_id: number; name: string; date: string; amount: number; kind: 'out' | 'card'; account_name: string | null; days: number }>
  | NeedBase<
      'card_due',
      'info',
      {
        account_id: number;
        name: string;
        mask: string | null;
        institution_name: string | null;
        due_date: string;
        days: number;
        minimum_payment: number | null;
        account_category: 'credit' | 'loan';
      }
    >
  | NeedBase<'new_recurring', 'info', NewRecurringOne | NewRecurringMany>
  /**
   * Release 3.19: "Netflix now charges $17.99. Update your amount?" (money in, `income`: "Acme Payroll came in at $2,410.00. …"). Yes / No, plus "Not now" (fingerprint = the charge id). Amounts are positive.
   */
  | NeedBase<'price_change', 'warn', { recurring_id: number; name: string; amount: number; current: number; income: boolean; date: string; transaction_id: number }>
  | NeedBase<'needs_category', 'info', { count: number; month_start: string }>
  | NeedBase<
      'big_purchase',
      'info',
      { event_id: number; transaction_id: number | null; name: string; amount: number; date: string; account_name: string | null; account_mask: string | null }
    >
  /** Built on the client from GET /api/recovery; "Not now" hides it for 14 days (a UI pref). */
  | NeedBase<'recovery_sheet', 'info', { status: Exclude<RecoveryState, 'active'> }>
  | UpdateBalanceNeed;

export type HomeAlertKind = HomeAlert['kind'];

export interface HomeCashAccount {
  id: number;
  name: string;
  mask: string | null;
  institution_name: string | null;
  balance: number;
  source: 'plaid' | 'manual';
  item_id: number | null;
  subtype: string | null;
}

export interface HomeBank {
  item_id: number;
  institution_name: string | null;
  kind: ItemKind;
  status: ItemStatus | string;
  error_code: string | null;
  last_synced_at: string | null;
}

/** The automatic backup's state, as POST /api/backup/auto/run returns it. */
export interface HomeBackupStatus {
  enabled: boolean;
  folder_name: string | null;
  last_at: string | null;
  last_error_at: string | null;
  last_error_code: BackupErrorCode | null;
}

/** Sections that fail on their own (null in the response and listed in `errors`). */
export type DashboardSection = 'cash' | 'budget' | 'coming_up' | 'months' | 'goals' | 'investments' | 'needs';

export interface DashboardSetup {
  plaid_configured: boolean;
  plaid_source: PlaidKeysSource;
  keys_rejected: boolean;
  visible_accounts: number;
  linked_items: number;
  pending_items: number;
}

export interface DashboardBudget {
  month: string;
  is_current: boolean;
  has_budget: boolean;
  /** Days after today in the month (0 on the last day). */
  days_left: number;
  planned: number;
  spent: number;
  /** planned − spent (negative when over). Goal and savings categories are left out. */
  left: number;
  next_paycheck: { date: string; name: string; amount: number } | null;
}

export interface ComingUpRow {
  key: string;
  recurring_id: number | null;
  date: string;
  /** The day it counts in checking (null for card rows). Rows sort by counted_on (else date), then date. */
  counted_on: string | null;
  name: string;
  kind: OccurrenceKind;
  account_name: string | null;
  /** Signed: negative = money out. */
  amount: number;
  status: 'upcoming' | 'late' | 'pending';
  /** A late or pending one carried to the top (still expected). */
  still_expected: boolean;
  /** Checking after this row; null for rows not from checking (card charges). */
  after: number | null;
  /**
   * The last counted row of its day: `after` is that day's balance (Bills and paychecks'). Only
   * these rows can say "Below your warning". False on every row of a day cut off by the 8-row limit.
   */
  day_end: boolean;
}

export interface DashboardComingUp {
  /** The calendar's checking account; null when none is chosen (then rows is empty). */
  account: { id: number; name: string; mask: string | null; institution_name: string | null } | null;
  from: string;
  to: string;
  /** Today's checking balance (null without an account). */
  balance: number | null;
  /** The low-balance warning amount (Settings → Alerts). */
  threshold: number;
  warning_on: boolean;
  /** About this much a day of everyday spending is counted (null when that setting is off). */
  daily_spend: number | null;
  rows: ComingUpRow[];
  /** Rows left out after the first 8. */
  more: number;
  /** Lowest day in [from, to] (earliest on ties). */
  low: { date: string; balance: number } | null;
  /** First day under the warning amount within 35 days (null when none or the warning is off); today = already below. */
  first_below: { date: string; balance: number } | null;
}

export interface DashboardMonth {
  month: string;
  came_in: number;
  went_out: number;
  left_over: number;
  /** false for the current month. */
  complete: boolean;
  /** false before the first transaction's month. */
  has_data: boolean;
  /** The first month with data, when history started after the 1st (shown muted, left out of the note). */
  partial: boolean;
}

export interface DashboardGoals {
  budget_ready: boolean;
  total_saved: number;
  in_progress: number;
  /** Unfinished goals, oldest first, at most 3. */
  top: { id: number; name: string; saved: number; target: number; status: 'on_track' | 'behind' | 'no_plan' }[];
}

export interface DashboardInvestments {
  has_accounts: boolean;
  worth: number;
  /** Since last month; null when no account has last month's value. */
  change: number | null;
  /** Names of accounts with no value from last month. */
  change_missing: string[];
}

export interface DashboardData {
  today: string;
  setup: DashboardSetup;
  banks: HomeBank[];
  cash: { total: number; updated_at: string | null; accounts: HomeCashAccount[] } | null;
  budget: DashboardBudget | null;
  coming_up: DashboardComingUp | null;
  /** The last 5 full months, then the current one. */
  months: DashboardMonth[] | null;
  goals: DashboardGoals | null;
  investments: DashboardInvestments | null;
  needs: HomeAlert[];
  /** "Not now": alert key → the fingerprint it was dismissed at. */
  dismissed: Record<string, string>;
  errors: DashboardSection[];
}

/**
 * POST /api/backup/auto/run: `ok` false means the backup ran and failed (see
 * backup.last_error_code). 409 "Choose a backup folder first."; 429 (with retry_after) when
 * one ran less than a minute ago.
 */
export interface AutoBackupRun {
  ok: boolean;
  backup: HomeBackupStatus;
}

export interface AutoSync {
  /** 0 = off. */
  hours: 0 | 3 | 6 | 12 | 24;
  next_at: string | null;
}

// ---------------------------------------------------------------- Settings redesign (D7, Release 3.7)
// Contract: SPEC.md "Settings redesign (D7, Release 3.7)".

export type AutoLockMinutes = 5 | 15 | 30 | 60;
export const AUTO_LOCK_CHOICES: AutoLockMinutes[] = [5, 15, 30, 60];
/** Longest "who to call for help" name (backend config.SUPPORT_CONTACT_MAX). */
export const SUPPORT_CONTACT_MAX = 60;

/** GET/PATCH /api/settings. `auto_lock_source` "env": nothing stored yet, the install's default applies. */
export interface AppSettings {
  auto_lock_minutes: number;
  auto_lock_choices: number[];
  auto_lock_source: 'vault' | 'env';
  password_changed_at: string | null;
  paycheck_notify: boolean;
}
export interface AppSettingsPatch {
  auto_lock_minutes?: AutoLockMinutes;
  paycheck_notify?: boolean;
}

/** One income item (a recurring item with money coming in). Writes go through /api/recurring. */
export interface PaycheckItem {
  id: number;
  name: string;
  amount: number;
  cadence: Cadence;
  next_date: string | null;
  account: { id: number; name: string; mask: string | null } | null;
  /** amount x (weekly 52/12, biweekly 26/12, semimonthly 2, monthly 1, quarterly 1/3, yearly 1/12, once 0). */
  per_month: number;
}
export interface Paychecks {
  items: PaycheckItem[];
  monthly_total: number;
  /** What Budget suggests for this month (the paychecks due this month). */
  this_month_total: number;
  income_mode: BudgetIncomeMode;
  notify: boolean;
}

/** Everything needed to put a deleted category back (Undo). Sent back as-is to POST /api/categories/restore. */
export interface CategorySnapshot {
  category: {
    id: string;
    name: string;
    hue: number;
    kind: CategoryKind;
    custom: boolean;
    hidden: boolean;
    position: number;
    group_id: number | null;
    target: CategoryTarget | null;
  };
  hand_set: { transaction_id: number; source: string }[];
  splits: number[];
  /** Release 3.17: `moved` = the row's one-time moves (restore: missing = 0). */
  budgets: { month: string; assigned: number; moved: number; removed: boolean; restart: boolean }[];
  bills: number[];
  rules: Rule[];
  flags: { savings_category: boolean; auto_map_key: string | null; excluded_plan: boolean };
}

/** PUT /api/alerts/settings (Release 3.7). */
export interface AlertsPatch {
  low?: { enabled?: boolean; value?: number };
  big?: { enabled?: boolean; value?: number };
  budget?: { enabled?: boolean };
  reminder?: { enabled?: boolean };
  newrec?: { enabled?: boolean };
  /** Release 3.19: ask when a bill's or paycheck's amount the user set changes. */
  price?: { enabled?: boolean };
}

/** POST /api/rules/preview (Release 3.7). */
export interface RulePreview {
  /** Past purchases the rule fits (splits excluded; other rules ignored). */
  matches: number;
  /** Those whose category would actually change (never hand-set ones). */
  would_change: number;
  /** Up to 20 would-change rows, newest first. */
  sample: {
    id: number;
    date: string;
    name: string;
    merchant: string | null;
    amount: number;
    from: { id: string; name: string; hue: number } | null;
    to: { id: string; name: string; hue: number };
  }[];
}

/** POST /api/category-groups/restore (Undo of deleting a group). */
export interface CategoryGroupSnapshot {
  id: number;
  name: string;
  position: number;
  category_ids: string[];
}

// ---------------------------------------------------------------- errors

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  /** Seconds until another attempt is allowed (429 only). */
  readonly retryAfter: number | null;
  readonly body: unknown;

  constructor(status: number, detail: string, body: unknown, retryAfter: number | null) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
    this.retryAfter = retryAfter;
  }

  get isLocked(): boolean {
    return this.status === 401;
  }
}

/** Human-readable message for anything thrown by the API layer. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return 'Iron Owl isn’t responding. Please wait a moment and try again.';
    return err.detail;
  }
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

function bodyField(e: unknown, key: string): unknown {
  return e instanceof ApiError && e.body && typeof e.body === 'object' && key in e.body ? (e.body as Record<string, unknown>)[key] : undefined;
}

/** The machine `code` of an error body (`{"detail": …, "code": …}`), if any. */
export function apiErrorCode(e: unknown): string | null {
  const c = bodyField(e, 'code');
  return typeof c === 'string' ? c : null;
}

/**
 * errorMessage, but a request the server's validation refused without a `code` (FastAPI 422s
 * carry only `{detail: [...]}`, e.g. a value that's far too long) gets plain words instead of
 * the validator's text.
 */
export function plainErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.status === 422 && !apiErrorCode(err)) {
    return 'Iron Owl couldn’t use what was typed. Please check it and try again.';
  }
  return errorMessage(err);
}

/** Wrong password or sheet: how many more tries before a "too many tries" wait (null if not sent). */
export function triesLeft(e: unknown): number | null {
  const n = bodyField(e, 'tries_left');
  return typeof n === 'number' && Number.isFinite(n) ? Math.max(0, Math.floor(n)) : null;
}

/** 422 `typo`: the 1-based groups whose check digit is wrong. */
export function typoGroups(e: unknown): number[] {
  const g = bodyField(e, 'groups');
  return Array.isArray(g) ? g.filter((n): n is number => typeof n === 'number' && n >= 1 && n <= 6) : [];
}

/** 409 `outdated`: the number of the sheet that works now. */
export function activeSheet(e: unknown): string | null {
  const s = bodyField(e, 'active_sheet');
  return typeof s === 'string' ? s : null;
}

/** 409 `needs_help` from POST /api/update/install: why and the code for Details. */
export function updateHelp(e: unknown): { reason: UpdateHelpReason; code: string } | null {
  const h = bodyField(e, 'help');
  if (!h || typeof h !== 'object') return null;
  const { reason, code } = h as { reason?: unknown; code?: unknown };
  return typeof reason === 'string' && typeof code === 'string' ? { reason: reason as UpdateHelpReason, code } : null;
}

/** 409 `update_rolled_back` from unlock: FinTrack is going back to this version (else null). */
export function rolledBackTo(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  if (e.detail !== 'update_rolled_back' && apiErrorCode(e) !== 'FT-UPD-05') return null;
  const v = bodyField(e, 'to_version');
  return typeof v === 'string' ? v : '';
}

function detailFrom(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return d;
    // FastAPI 422 validation errors: [{loc, msg, type}, ...]
    if (Array.isArray(d)) {
      const msgs = d
        .map((e) => (e && typeof e === 'object' && 'msg' in e ? String((e as { msg: unknown }).msg) : ''))
        .filter(Boolean);
      if (msgs.length) return msgs.join('; ');
    }
  }
  return fallback;
}

// ---------------------------------------------------------------- locked signal

type LockedListener = (why: SessionEnd) => void;
const lockedListeners = new Set<LockedListener>();

/**
 * Subscribe to "the server says this window's session is over": `locked` (the vault locked or the
 * session expired) or `moved` (another window took the session over). Returns an unsubscribe fn.
 */
export function onLocked(fn: LockedListener): () => void {
  lockedListeners.add(fn);
  return () => {
    lockedListeners.delete(fn);
  };
}

function emitLocked(why: SessionEnd) {
  setSessionToken(null);
  lockedListeners.forEach((fn) => fn(why));
}

/** A 401 that ends the session: `{"detail":"moved"}` means another window has it now. */
function endSession(detail: string, passwordAttempt: boolean | undefined) {
  if (detail === 'moved') emitLocked('moved');
  else if (!passwordAttempt || detail === 'locked') emitLocked('locked');
}

// ---------------------------------------------------------------- lost connection

/**
 * Requests that got no answer at all (network error), in a row. Two in a row (page requests or
 * heartbeats) tell the app to check whether FinTrack is still running (FT-RUN-01).
 */
let missedInARow = 0;
const LOST_AFTER = 2;
type LostListener = () => void;
const lostListeners = new Set<LostListener>();

/** Subscribe to "two requests in a row got no answer". Returns an unsubscribe fn. */
export function onConnectionLost(fn: LostListener): () => void {
  lostListeners.add(fn);
  return () => {
    lostListeners.delete(fn);
  };
}

function noteAnswered() {
  missedInARow = 0;
}

function noteMissed() {
  missedInARow += 1;
  if (missedInARow >= LOST_AFTER) lostListeners.forEach((fn) => fn());
}

// ---------------------------------------------------------------- session token

/**
 * Second half of the session, paired with the HttpOnly cookie. Browsers send
 * cookies to every port on 127.0.0.1, so another local server could receive
 * the cookie; sessionStorage is isolated per origin (including port) and per
 * tab, so that server can't read this token. Kept in sessionStorage (not just
 * memory) so a page reload doesn't force a re-unlock; a new tab must unlock.
 */
const SESSION_TOKEN_KEY = 'ft_session_token';
let sessionToken: string | null = null;
try {
  sessionToken = sessionStorage.getItem(SESSION_TOKEN_KEY);
} catch {
  /* storage blocked: memory only */
}

/** Bumped whenever this window's session changes (so a heartbeat sent before can be ignored). */
let sessionGen = 0;
export function sessionGeneration(): number {
  return sessionGen;
}

/** Adopt a session token handed over by another window ("Open here"), or drop this window's. */
export function setSessionToken(token: string | null) {
  if (token !== sessionToken) sessionGen += 1;
  sessionToken = token;
  try {
    if (token) sessionStorage.setItem(SESSION_TOKEN_KEY, token);
    else sessionStorage.removeItem(SESSION_TOKEN_KEY);
  } catch {
    /* storage blocked: memory only */
  }
}

async function startSession<T extends SessionResponse>(p: Promise<T>): Promise<T> {
  const res = await p;
  setSessionToken(res.session_token ?? null);
  return res;
}

// ---------------------------------------------------------------- core request

interface RequestOptions {
  /**
   * For password-bearing endpoints (unlock, setup, change-password), a 401
   * means "wrong password", not "session expired". Only `{"detail":"locked"}`
   * then triggers the global locked state.
   */
  passwordAttempt?: boolean;
  signal?: AbortSignal;
  /** A probe (health): a network error here doesn't count toward "connection lost". */
  probe?: boolean;
  /** fetch keepalive (the "window closing" beacon, sent from pagehide). */
  keepalive?: boolean;
  /** Don't send `X-FinTrack-Session` (the closing beacon needs only the CSRF and tab headers). */
  noSession?: boolean;
  /**
   * A 401 doesn't end the session here (polling while FinTrack restarts for an update: the old
   * session is gone by design, and the install screen decides what comes next).
   */
  quiet401?: boolean;
}

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

async function request<T>(method: Method, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'X-FinTrack': '1',
    'X-FinTrack-Tab': getTabId(),
  };
  if (sessionToken && !opts.noSession) headers['X-FinTrack-Session'] = sessionToken;
  const init: RequestInit = {
    method,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    signal: opts.signal,
  };
  if (opts.keepalive) init.keepalive = true;
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    if (!opts.probe && !opts.keepalive) noteMissed();
    throw new ApiError(0, 'Network error', null, null);
  }
  if (!opts.probe) noteAnswered();

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    const detail = detailFrom(data, res.statusText || `HTTP ${res.status}`);
    let retryAfter: number | null = null;
    if (res.status === 429) {
      const fromBody =
        data && typeof data === 'object' && 'retry_after' in data ? Number((data as { retry_after: unknown }).retry_after) : NaN;
      const fromHeader = Number(res.headers.get('Retry-After'));
      retryAfter = Number.isFinite(fromBody) ? fromBody : Number.isFinite(fromHeader) ? fromHeader : null;
    }
    const err = new ApiError(res.status, detail, data, retryAfter);
    if (res.status === 401 && !opts.quiet401) endSession(detail, opts.passwordAttempt);
    throw err;
  }

  return data as T;
}

/** Shared transaction filter params (`view=all` is the default, so it's left out). */
function filterParams(q: TransactionFilters) {
  return {
    account_id: q.account_id,
    search: q.search,
    start: q.start,
    end: q.end,
    category: q.category,
    tag: q.tag,
    view: q.view === 'all' ? undefined : q.view,
  };
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

/**
 * Non-JSON transfers (CSV export, backup download, restore upload). Same auth
 * headers and error handling as `request`, but the body is a Blob or FormData.
 */
async function requestRaw(
  method: Method,
  path: string,
  body: FormData | string | undefined,
  opts: RequestOptions & { json?: boolean } = {},
): Promise<Blob | unknown> {
  const headers: Record<string, string> = { 'X-FinTrack': '1', 'X-FinTrack-Tab': getTabId() };
  if (sessionToken) headers['X-FinTrack-Session'] = sessionToken;
  if (typeof body === 'string') headers['Content-Type'] = 'application/json';
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body, credentials: 'same-origin', cache: 'no-store', signal: opts.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    noteMissed();
    throw new ApiError(0, 'Network error', null, null);
  }
  noteAnswered();
  if (!res.ok) {
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      /* not JSON */
    }
    const detail = detailFrom(data, res.statusText || `HTTP ${res.status}`);
    const retry = res.status === 429 ? Number(res.headers.get('Retry-After')) : NaN;
    const err = new ApiError(res.status, detail, data, Number.isFinite(retry) ? retry : null);
    if (res.status === 401) endSession(detail, opts.passwordAttempt);
    throw err;
  }
  return opts.json ? res.json() : res.blob();
}

/** Save a Blob as a file via a temporary object URL. */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------- endpoints

/** Run a request with a time limit: its signal aborts after `ms`. */
async function withTimeout<T>(ms: number, run: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const ctl = new AbortController();
  const timer = window.setTimeout(() => ctl.abort(), ms);
  try {
    return await run(ctl.signal);
  } finally {
    window.clearTimeout(timer);
  }
}

export const api = {
  /** GET /api/health (no session). Same as `app.health`: 5 s limit, never counts toward "connection lost". */
  health: () => withTimeout(5000, (signal) => request<Health>('GET', '/api/health', undefined, { signal, probe: true, quiet401: true })),
  app: {
    /** Is FinTrack answering at all? 5 s limit; any failure (the time limit too) throws. */
    health: () => withTimeout(5000, (signal) => request<Health>('GET', '/api/health', undefined, { signal, probe: true })),
    /** Heartbeat: every 30 s and when the window comes back to the front. Not activity. */
    alive: () => request<Presence>('POST', '/api/app/alive', {}),
    /** From pagehide: the window is going away (the server locks after a short grace unless it comes back). */
    // No session header: the beacon outlives the page, and the server decides by tab id alone.
    closing: () => request<void>('POST', '/api/app/closing', {}, { keepalive: true, noSession: true }),
    /** Move this window's session to window `to_tab` (that window asked: "Open here"). */
    handoff: (to_tab: string) => request<{ session_token: string }>('POST', '/api/app/handoff', { to_tab }),
  },
  auth: {
    /** `timeoutMs`: the first read at start gives up after 20 s (then "FinTrack isn't running"). */
    status: (timeoutMs?: number) =>
      timeoutMs
        ? withTimeout(timeoutMs, (signal) => request<AuthStatus>('GET', '/api/auth/status', undefined, { signal }))
        : request<AuthStatus>('GET', '/api/auth/status'),
    setup: (password: string) =>
      startSession(request<SetupResponse>('POST', '/api/auth/setup', { password }, { passwordAttempt: true })),
    unlock: (password: string) =>
      startSession(request<SessionResponse>('POST', '/api/auth/unlock', { password }, { passwordAttempt: true })),
    lock: (reason?: 'idle' | 'manual') =>
      request<OkResponse>('POST', '/api/auth/lock', reason ? { reason } : undefined).finally(() => setSessionToken(null)),
    /** Release 3.7: `backup` = the automatic backup forced right after (null when no folder is set). */
    changePassword: (current_password: string, new_password: string) =>
      startSession(
        request<SessionResponse & { backup?: { ok: boolean } | null }>(
          'POST',
          '/api/auth/change-password',
          { current_password, new_password },
          { passwordAttempt: true },
        ),
      ),
    /**
     * Forgot password, step 1 (no session): are these the numbers of this computer's sheet?
     * 422 bad_format / typo (+groups), 401 no_match (+tries_left), 409 outdated (+active_sheet) /
     * unusable / no_recovery / not_initialized, 429. A 401 here is never "locked".
     */
    recoverCheck: (code: string) =>
      request<{ ok: true; sheet: string }>('POST', '/api/auth/recover/check', { code }, { passwordAttempt: true }),
    /** Step 2: set a new password with the sheet; unlocks like `unlock` (the sheet keeps working). */
    recover: (code: string, new_password: string) =>
      startSession(request<SessionResponse>('POST', '/api/auth/recover', { code, new_password }, { passwordAttempt: true })),
  },

  recovery: {
    get: () => request<RecoveryStatus>('GET', '/api/recovery'),
    /** A new sheet, pending until `activate` (the old one keeps working until then). */
    create: (current_password: string) =>
      request<RecoveryPending>('POST', '/api/recovery', { current_password }, { passwordAttempt: true }),
    activate: (pending_id: string) => request<RecoveryStatus>('POST', '/api/recovery/activate', { pending_id }),
    /** Setup's "I have printed this sheet": marks the sheet made at setup as confirmed. */
    confirm: (sheet: string) => request<RecoveryStatus>('POST', '/api/recovery/confirm', { sheet }),
  },

  accounts: {
    list: () => request<Account[]>('GET', '/api/accounts'),
    create: (body: NewManualAccount) => request<Account>('POST', '/api/accounts', body),
    update: (id: number, patch: AccountPatch) => request<Account>('PATCH', `/api/accounts/${id}`, patch),
    remove: (id: number) => request<void>('DELETE', `/api/accounts/${id}`),
    history: (id: number, days = 365) => request<BalancePoint[]>('GET', `/api/accounts/${id}/history${qs({ days })}`),
  },

  summary: () => request<Summary>('GET', '/api/summary'),
  networthHistory: (days = 365) => request<NetWorthPoint[]>('GET', `/api/networth/history${qs({ days })}`),

  transactions: (q: TransactionQuery = {}) =>
    request<TransactionPage>(
      'GET',
      `/api/transactions${qs({
        ...filterParams(q),
        limit: q.limit ?? 50,
        cursor: q.cursor,
        offset: q.cursor ? undefined : (q.offset ?? 0),
      })}`,
    ),
  /** Release 3.3 */
  transactionsSummary: (q: TransactionFilters = {}) =>
    request<TransactionSummary>('GET', `/api/transactions/summary${qs(filterParams(q))}`),
  /** Release 3.3: ids in list order (max 1000, `truncated` beyond that). */
  transactionIds: (q: TransactionFilters & { needs_category?: boolean } = {}) =>
    request<{ ids: number[]; total: number; truncated: boolean }>(
      'GET',
      `/api/transactions/ids${qs({ ...filterParams(q), needs_category: q.needs_category ? 'true' : undefined })}`,
    ),
  /** Release 3.3: set (or with null, reset to automatic) the category of up to 1000 transactions. */
  bulkSetCategory: (ids: number[], category: string | null) =>
    request<BulkCategoryResult>('PATCH', '/api/transactions', { ids, category }),
  /** Release 3.3: Undo. Puts categories and their sources back. */
  restoreCategories: (items: CategoryState[]) => request<{ items: Transaction[] }>('POST', '/api/transactions/restore', { items }),
  transactionRelated: (id: number) => request<TransactionRelated>('GET', `/api/transactions/${id}/related`),

  holdings: (accountId?: number) => request<Holding[]>('GET', `/api/holdings${qs({ account_id: accountId })}`),

  plaid: {
    status: () => request<PlaidStatus>('GET', '/api/plaid/status'),
    /** With itemId → update mode (re-authenticate an existing item). */
    linkToken: (kind: ItemKind, itemId?: number) =>
      request<{ link_token: string }>('POST', '/api/plaid/link-token', itemId === undefined ? { kind } : { kind, item_id: itemId }),
    /** Release 2: no longer syncs; the item is `pending` until `importAccounts`. */
    exchange: (public_token: string, kind: ItemKind) =>
      request<ExchangeResponse>('POST', '/api/plaid/exchange', { public_token, kind }),
    discoveredAccounts: (itemId: number) => request<DiscoveredAccount[]>('GET', `/api/plaid/items/${itemId}/accounts`),
    /** Import the chosen accounts (also "Manage accounts" later) and sync the item. */
    importAccounts: (itemId: number, plaid_account_ids: string[]) =>
      request<ImportResponse>('POST', `/api/plaid/items/${itemId}/import`, { plaid_account_ids }),
    items: () => request<PlaidItem[]>('GET', '/api/plaid/items'),
    sync: (itemId?: number) => request<SyncResponse>('POST', '/api/plaid/sync', itemId === undefined ? {} : { item_id: itemId }),
    removeItem: (id: number) => request<void>('DELETE', `/api/plaid/items/${id}`),
  },

  /** Release 3.4: Plaid keys kept encrypted in the vault (Settings → Bank connection). The secret is write-only. */
  plaidKeys: {
    get: () => request<PlaidKeysStatus>('GET', '/api/plaid/keys'),
    /** Candidate keys are checked and not stored; no argument checks the active keys. */
    test: (keys?: PlaidKeysTestInput) => request<PlaidKeysTest>('POST', '/api/plaid/keys/test', keys ?? {}),
    save: (keys: PlaidKeysInput, current_password: string) =>
      request<PlaidKeysStatus>('PUT', '/api/plaid/keys', { ...keys, current_password }, { passwordAttempt: true }),
    remove: (current_password: string) =>
      request<void>('POST', '/api/plaid/keys/delete', { current_password }, { passwordAttempt: true }),
  },

  // ---------------------------------------------------------------- Release 2

  categories: {
    list: () => request<TxnCategory[]>('GET', '/api/categories'),
    /** Release 3.7: `group_id` (422 "unknown group"). */
    create: (body: { name: string; hue?: number; kind: CategoryKind; group_id?: number | null }) => request<TxnCategory>('POST', '/api/categories', body),
    update: (id: string, patch: CategoryPatch) =>
      request<TxnCategory>('PATCH', `/api/categories/${encodeURIComponent(id)}`, patch),
    /** Release 3.7: also deletes the rules that use it; take a `snapshot` first for Undo. */
    remove: (id: string) => request<void>('DELETE', `/api/categories/${encodeURIComponent(id)}`),
    snapshot: (id: string) => request<CategorySnapshot>('GET', `/api/categories/${encodeURIComponent(id)}/snapshot`),
    /** Undo of Delete: 201, or 409 when the id or name was taken meanwhile. */
    restore: (snap: CategorySnapshot) => request<TxnCategory>('POST', '/api/categories/restore', snap),
  },

  updateTransaction: (id: number, patch: TransactionPatch) => request<Transaction>('PATCH', `/api/transactions/${id}`, patch),

  /** CSV of every transaction matching the filters (limit/offset ignored). */
  exportTransactionsCsv: (q: TransactionFilters = {}) =>
    requestRaw(
      'GET',
      `/api/transactions/export.csv${qs(filterParams(q))}`,
      undefined,
    ) as Promise<Blob>,

  budgets: {
    get: (month?: string) => request<BudgetMonth>('GET', `/api/budgets${qs({ month })}`),
    categoryTransactions: (month: string, category: string, q: { limit?: number; offset?: number } = {}) =>
      request<BudgetCategoryTransactionPage>('GET', `/api/budgets/${encodeURIComponent(month)}/categories/${encodeURIComponent(category)}/transactions${qs({ limit: q.limit ?? 50, offset: q.offset ?? 0 })}`),
    save: (month: string, body: BudgetSave) => request<BudgetMonth>('PUT', `/api/budgets/${month}`, body),
    /** Which accounts fund the budget. Returns the current month. */
    setAccounts: (account_ids: number[]) => request<BudgetMonth>('PUT', '/api/budgets/accounts', { account_ids }),
    review: (month?: string) => request<SpendingReview>('GET', `/api/spending/review${qs({ month })}`),
    /** Release 3.6: the income switch and the Emergency savings category. Returns today's month. */
    settings: (body: BudgetSettingsInput) => request<BudgetMonth>('PUT', '/api/budgets/settings', body),
    setupInfo: () => request<BudgetSetupInfo>('GET', '/api/budgets/setup'),
    setup: (body: BudgetSetupInput) => request<BudgetMonth>('POST', '/api/budgets/setup', body),
    /** A new spending category with a plan in `month`, in one step. Undo: api.categories.remove(id). */
    addCategory: (month: string, body: { name: string; group_id: number | null; plan: number }) =>
      request<{ category: TxnCategory; month: BudgetMonth }>('POST', `/api/budgets/${month}/categories`, body),
    /** Release 3.14: the category's row in `month` exactly, read before a save so Undo can put it back. */
    rowState: (month: string, category: string) =>
      request<{ state: BudgetRowState | null }>('GET', `/api/budgets/${encodeURIComponent(month)}/rows/${encodeURIComponent(category)}`),
    /** Release 3.14: put rows of one month back exactly, all or nothing (1-10 rows, each category once).
     *  `state` null = delete the row; `expected` = the row right after the change being undone (from
     *  `rowState` after the save). 409 when a row isn't `expected` any more ("This plan was changed
     *  since, so it wasn't undone."); 422 for past months or when it would lower Ready to Assign below zero. */
    putBackRows: (month: string, rows: BudgetRowRestore[]) =>
      request<BudgetMonth>('PUT', `/api/budgets/${encodeURIComponent(month)}/rows`, { rows }),
  },

  recurring: {
    list: () => request<RecurringItem[]>('GET', '/api/recurring'),
    create: (body: RecurringInput) => request<RecurringItem>('POST', '/api/recurring', body),
    update: (id: number, patch: RecurringPatch) => request<RecurringItem>('PATCH', `/api/recurring/${id}`, patch),
    remove: (id: number) => request<void>('DELETE', `/api/recurring/${id}`),
    detect: () => request<{ suggested: number }>('POST', '/api/recurring/detect', {}),
    /** Release 3.4: suggested items with the evidence ("From checking on the 12th · Jul, Aug and Sep"). */
    candidates: () => request<RecurringCandidate[]>('GET', '/api/recurring/candidates'),
    /** Release 3.4: the Add dialog's "Which budget category?" pre-fill, from the merchant's past transactions. */
    /** A POST body, so the typed name never appears in a URL; only money in (1) vs out (-1) goes along. */
    categoryGuess: (q: { name: string; amount_sign?: 1 | -1 | null; account_id?: number | null }) =>
      request<{ category_id: string | null; category_name: string | null }>('POST', '/api/recurring/category-guess', {
        name: q.name,
        amount_sign: q.amount_sign ?? null,
        account_id: q.account_id ?? null,
      }),
    /** Release 3.4: everything needed to put an item back exactly (Undo of Delete). */
    snapshot: (id: number) => request<RecurringSnapshot>('GET', `/api/recurring/${id}/snapshot`),
    restore: (s: RecurringSnapshot) => request<RecurringItem>('POST', '/api/recurring/restore', s),
    /** "Move all future ones too": re-anchor the series on `to` (only from its next scheduled date). */
    reschedule: (id: number, body: { from_base: string; to: string }) =>
      request<RescheduleResult>('POST', `/api/recurring/${id}/reschedule`, body),
    /** Full override row for one occurrence (an all-default body deletes it). */
    setOccurrence: (id: number, base: string, body: OccurrenceOverrideInput) =>
      request<OccurrenceChange>('PUT', `/api/recurring/${id}/occurrences/${base}`, body),
    clearOccurrence: (id: number, base: string) => request<void>('DELETE', `/api/recurring/${id}/occurrences/${base}`),
    /** Release 3.19: No to "now charges $X. Update your amount?" (`undo` asks again). Yes is `update(id, { amount })`. */
    priceAnswer: (id: number, body: { transaction_id: number; answer: 'no' | 'undo' }) =>
      request<void>('POST', `/api/recurring/${id}/price-question`, body),
  },

  forecast: {
    get: (end?: string) => request<Forecast>('GET', `/api/forecast${qs({ end })}`),
    setAccount: (account_id: number) => request<Forecast>('PUT', '/api/forecast/account', { account_id }),
    /** Release 3.4: the cash forecast calendar (server-side projection, statuses, months). */
    calendar: (q: { from?: string; to?: string } = {}) => request<ForecastCalendar>('GET', `/api/forecast/calendar${qs(q)}`),
    settings: () => request<ForecastSettings>('GET', '/api/forecast/settings'),
    updateSettings: (p: ForecastSettingsPatch) => request<ForecastSettings>('PATCH', '/api/forecast/settings', p),
  },

  goals: {
    get: () => request<GoalsState>('GET', '/api/goals'),
    /** 409 while the Budget needs setup / a second emergency fund / a taken name; 422 `not_enough_unplanned`. */
    create: (body: GoalInput) => request<GoalsState & { goal_id: number }>('POST', '/api/goals', body),
    update: (id: number, patch: GoalPatch) => request<GoalsState>('PATCH', `/api/goals/${id}`, patch),
    /** "I spent it" is reason `spent`; the Edit window's Delete is `deleted`. */
    remove: (id: number, reason: 'deleted' | 'spent') =>
      request<GoalDeleted>('DELETE', `/api/goals/${id}${qs({ reason })}`),
    /**
     * The Undo right after adding a goal: a taken-over category (Emergency savings) goes back exactly as it
     * was; 409 once its plan changed on the Budget page or the month changed.
     */
    undoAdd: (id: number) => request<GoalAddUndone>('DELETE', `/api/goals/${id}${qs({ reason: 'undo_add' })}`),
    /** Undo of a delete (once). 409 when the month changed, it was already undone or the category is gone. */
    restore: (undo: GoalUndo) => request<GoalsState>('POST', '/api/goals/restore', undo),
  },

  investments: () => request<Investments>('GET', '/api/investments'),

  rules: {
    list: () => request<Rule[]>('GET', '/api/rules'),
    /** Release 3.7: `position` inserts there (the rest shift down; out of range: last); `first: true` = position 0. */
    create: (body: RuleInput & { position?: number; first?: boolean }) => request<Rule>('POST', '/api/rules', body),
    update: (id: number, patch: Partial<RuleInput>) => request<Rule>('PATCH', `/api/rules/${id}`, patch),
    remove: (id: number) => request<void>('DELETE', `/api/rules/${id}`),
    reorder: (ids: number[]) => request<Rule[]>('POST', '/api/rules/reorder', { ids }),
    /** Release 3.7: `rule_id` previews an edit at that rule's place; `sample` = up to 20 would-change rows. */
    preview: (body: RuleInput & { rule_id?: number }) => request<RulePreview>('POST', '/api/rules/preview', body),
  },

  /** Vault copies set aside by earlier restores; each still opens with its old password. */
  previousVaults: {
    list: () => request<PreviousVault[]>('GET', '/api/backup/previous'),
    remove: (id: string, password: string) =>
      request<void>('POST', `/api/backup/previous/${encodeURIComponent(id)}/delete`, { password }, { passwordAttempt: true }),
  },

  alerts: {
    settings: () => request<AlertSetting[]>('GET', '/api/alerts/settings'),
    updateSetting: (key: AlertKey, patch: { enabled?: boolean; value?: number }) =>
      request<AlertSetting>('PUT', `/api/alerts/settings/${key}`, patch),
    /**
     * Release 3.7 (Settings › Alerts): several at once, atomic. `low.enabled` also sets
     * `low_ahead`, and `reminder.enabled` also sets `due`. Returns the full list.
     */
    updateMany: (body: AlertsPatch) => request<AlertSetting[]>('PUT', '/api/alerts/settings', body),
    events: (limit = 50) => request<AlertEvent[]>('GET', `/api/alerts/events${qs({ limit })}`),
    markRead: () => request<OkResponse>('POST', '/api/alerts/events/read', {}),
    clear: () => request<void>('DELETE', '/api/alerts/events'),
  },

  backup: {
    /** Encrypted .ftbackup file; the password must match the vault password. */
    download: (password: string) =>
      requestRaw('POST', '/api/backup', JSON.stringify({ password }), { passwordAttempt: true }) as Promise<Blob>,
    /**
     * Works without a session only before setup; unlocks the restored vault.
     * Replacing an existing vault also needs its current password.
     */
    restore: async (file: File, password: string, currentPassword?: string) => {
      const form = new FormData();
      form.append('file', file);
      form.append('password', password);
      if (currentPassword !== undefined) form.append('current_password', currentPassword);
      return startSession(requestRaw('POST', '/api/restore', form, { passwordAttempt: true, json: true }) as Promise<SessionResponse>);
    },
  },

  // ---------------------------------------------------------------- Release 3

  categoryGroups: {
    list: () => request<CategoryGroup[]>('GET', '/api/category-groups'),
    create: (name: string) => request<CategoryGroup>('POST', '/api/category-groups', { name }),
    rename: (id: number, name: string) => request<CategoryGroup>('PATCH', `/api/category-groups/${id}`, { name }),
    remove: (id: number) => request<void>('DELETE', `/api/category-groups/${id}`),
    reorder: (ids: number[]) => request<CategoryGroup[]>('POST', '/api/category-groups/reorder', { ids }),
    starter: () => request<CategoryGroup[]>('POST', '/api/category-groups/starter', {}),
    /** Release 3.7: Undo of Delete. Returns every group, in order. */
    restore: (g: CategoryGroupSnapshot) => request<CategoryGroup[]>('POST', '/api/category-groups/restore', g),
  },

  fundTargets: (month: string) => request<FundTargetsResult>('POST', `/api/budgets/${month}/fund-targets`, {}),

  setSplits: (transactionId: number, splits: SplitInput[]) =>
    request<Transaction>('PUT', `/api/transactions/${transactionId}/splits`, { splits }),

  recategorize: {
    preview: (m: RecategorizeMatch) => request<RecategorizePreview>('POST', '/api/transactions/recategorize/preview', m),
    apply: (m: RecategorizeMatch & { include_manual: boolean }) =>
      request<{ changed: number }>('POST', '/api/transactions/recategorize', m),
  },

  tags: {
    list: () => request<Tag[]>('GET', '/api/tags'),
    create: (name: string, hue?: number) => request<Tag>('POST', '/api/tags', hue === undefined ? { name } : { name, hue }),
    update: (id: number, patch: { name?: string; hue?: number }) => request<Tag>('PATCH', `/api/tags/${id}`, patch),
    remove: (id: number) => request<void>('DELETE', `/api/tags/${id}`),
    setOnTransaction: (transactionId: number, tag_ids: number[]) =>
      request<Transaction>('PUT', `/api/transactions/${transactionId}/tags`, { tag_ids }),
  },

  reports: {
    monthly: (months = 12) => request<MonthlyReport>('GET', `/api/reports/monthly${qs({ months })}`),
    category: (id: string, start: string, end: string, months = 6) =>
      request<CategoryReport>('GET', `/api/reports/category/${encodeURIComponent(id)}${qs({ start, end, months })}`),
    habits: (month: string, under = 15) => request<HabitsReport>('GET', `/api/reports/habits${qs({ month, under })}`),
    /** Release 3.14: the same days this year and last year. */
    yoy: (mode: YoyMode, month?: string) => request<YoyReport>('GET', `/api/reports/yoy${qs({ mode, month })}`),
    /** Release 3.14: non-bill recurring money-out items, with price changes. */
    subscriptions: () => request<SubscriptionsReport>('GET', '/api/reports/subscriptions'),
  },

  loans: {
    payoff: (accountId: number, extra = 0, minimum?: number) =>
      request<PayoffProjection>('GET', `/api/loans/${accountId}/payoff${qs({ extra, minimum })}`),
    plan: (body: DebtPlanInput) => request<DebtPlan>('POST', '/api/debt/plan', body),
  },

  autoBackup: {
    get: () => request<AutoBackup>('GET', '/api/backup/auto'),
    /** `create`: make a missing folder first (Release 3.7, "Turn on"). */
    set: (dir: string | null, keep: number, create = false) =>
      request<AutoBackup>('PUT', '/api/backup/auto', create ? { dir, keep, create: true } : { dir, keep }),
    /** OneDrive/Iron Owl backups when OneDrive exists, else Documents/Iron Owl backups (or an existing older "FinTrack backups"); `dir` null when neither is a local folder. */
    suggestedFolder: () => request<{ dir: string | null; exists: boolean }>('GET', '/api/backup/auto/suggested-folder'),
    /** The Windows folder picker, run by the server. `dir` null: cancelled. 409 busy; 501 unavailable. */
    pickFolder: () => request<{ dir: string | null }>('POST', '/api/backup/auto/pick-folder', {}),
    /** Home "Back up now": runs one backup right away. 409 when no folder is set; rate-limited server-side. */
    run: () => request<AutoBackupRun>('POST', '/api/backup/auto/run', {}),
  },

  // ---------------------------------------------------------------- Home v2 (SPEC "Home v2")

  dashboard: () => request<DashboardData>('GET', '/api/dashboard'),
  /** "Not now": hide `key` until its fingerprint changes. */
  homeDismiss: (key: string, fingerprint: string) =>
    request<void>('PUT', `/api/home/dismissals/${encodeURIComponent(key)}`, { fingerprint }),
  homeUndismiss: (key: string) => request<void>('DELETE', `/api/home/dismissals/${encodeURIComponent(key)}`),

  /** Updates (design D5). Every route needs a session; status and progress don't count as activity. */
  update: {
    status: () => request<UpdateStatus>('GET', '/api/update/status'),
    /**
     * Look in Downloads now (the server answers from a 30 s cache when asked again sooner). In
     * the GitHub source this only checks GitHub when the daily check is due.
     */
    scan: () => request<UpdateStatus>('POST', '/api/update/scan', {}),
    /** GitHub source: Settings "Check now" (at most once a minute; the switch doesn't matter). */
    checkNow: () => request<UpdateStatus>('POST', '/api/update/scan', { now: true }),
    /** GitHub source: Settings "Check for updates once a day". 409 disabled in other sources. */
    setAutoCheck: (on: boolean) => request<UpdateStatus>('PUT', '/api/update/auto-check', { on }),
    /** A file the user picked in Settings: 413 FT-UPD-BIG, 409 disabled, 429 busy. */
    checkFile: async (file: File) => {
      const form = new FormData();
      form.append('file', file, file.name);
      return (await requestRaw('POST', '/api/update/file', form, { json: true })) as UpdateCheck;
    },
    /** Not now: hide the banner for this offer until tomorrow. 404 stale_offer. */
    dismiss: (id: string) => request<UpdateStatus>('POST', '/api/update/dismiss', { id }),
    /** 202 with the first step. 409 needs_help (+help) / busy / disabled; 404 stale_offer. */
    install: (id: string) => request<UpdateProgress>('POST', '/api/update/install', { id }),
    /**
     * Polled while installing. 204 when nothing is running. `quiet`: while FinTrack restarts, a
     * 401 or no answer is expected and must not lock the window or count as "connection lost".
     */
    progress: (quiet = false) =>
      withTimeout(5000, (signal) =>
        request<UpdateProgress | undefined>('GET', '/api/update/progress', undefined, { signal, probe: quiet, quiet401: quiet }),
      ),
    /** The result message was shown. */
    ackResult: (at: string) => request<void>('POST', '/api/update/result/ack', { at }),
    /** Optional: go back to the previous version (same data format only). */
    rollback: (password: string) =>
      request<{ to_version: string }>('POST', '/api/update/rollback', { password }, { passwordAttempt: true }),
  },

  autoSync: {
    get: () => request<AutoSync>('GET', '/api/plaid/auto-sync'),
    set: (hours: AutoSync['hours']) => request<AutoSync>('PUT', '/api/plaid/auto-sync', { hours }),
  },

  // ---------------------------------------------------------------- Release 3.7 (Settings, D7)

  settings: {
    get: () => request<AppSettings>('GET', '/api/settings'),
    update: (patch: AppSettingsPatch) => request<AppSettings>('PATCH', '/api/settings', patch),
    /** Who to call for help ("" = no one named; at most SUPPORT_CONTACT_MAX characters). Read
     *  back through `auth.status().support_contact` (also while locked). */
    setSupportContact: (contact: string) =>
      request<{ support_contact: string | null }>('PUT', '/api/settings/support-contact', { contact }),
  },

  paychecks: () => request<Paychecks>('GET', '/api/paychecks'),

  // ---- Release 3.10 pair 1 (Accounts + sidebar): add api functions here ----
  /** Release 3.10 Accounts: kind-based manual add, balance with exact Undo, remove with restore. */
  accountsV2: {
    /** A manual account from the Add window's kinds (category and subtype follow the kind). */
    create: (body: NewAccountByKind) => request<Account>('POST', '/api/accounts', body),
    /** Manual accounts only; debts are the amount owed (positive). Undo: `undoBalance`. */
    setBalance: (id: number, balance: number) => request<BalanceSaved>('PUT', `/api/accounts/${id}/balance`, { balance }),
    /** Puts back the balance and today's snapshot exactly (today only, single use; 409 otherwise). */
    undoBalance: (token: string) => request<Account>('POST', '/api/accounts/balance/undo', { token }),
    /** Manual accounts only (400 for bank-connected ones). Undo: `restore`. */
    remove: (id: number) => request<AccountRemoved>('DELETE', `/api/accounts/${id}`),
    /** Puts a removed manual account back with its history and links (201; 409 when the token is used or old). */
    restore: (token: string) => request<Account>('POST', '/api/accounts/restore', { token }),
  },
  /** Settings › Banks "Bank connections used: N of 10" › Change…: the real count from Plaid's dashboard. */
  connectionsUsed: {
    /** A whole number from the connections there are now (`items_now`) up to `items_limit`; 422 otherwise. */
    set: (count: number) => request<ConnectionsUsed>('PUT', '/api/plaid/items-linked', { count }),
    /** Iron Owl 2.0.0: count connections against Plaid's Trial limit (on) or not (off: `items_limit` 0). */
    setCap: (on: boolean) => request<ConnectionsUsed>('PUT', '/api/plaid/items-cap', { on }),
  },

  // ---- Release 3.10 pair 2 (Reports tabs, Spending, Debt): add api functions here ----

  /** Reports › Spending (reports-spending design): one month, the last 6 months, categories and stores. */
  spendingReport: (month?: string) => request<SpendingReport>('GET', `/api/reports/spending${qs({ month })}`),

  /** Reports › Paying off debt (Release 3.10). */
  debt: {
    /** Leave out `account_ids` for every visible debt with a minimum (the rest come back in `skipped`). */
    plan: (body: DebtPlanRequest) => request<DebtPlanV2>('POST', '/api/debt/plan', body),
    /** `ids` left out: every visible loan or card. */
    progress: (ids?: number[]) => request<DebtProgress>('GET', `/api/debt/progress${qs({ ids: ids?.join(',') })}`),
    budget: () => request<DebtBudgetState>('GET', '/api/debt/budget'),
    /** Loans only (`account_ids` of kind loan or mortgage). 409 setup not done; 422 `not_enough_unplanned` (+`not_planned`), or a card. */
    setBudget: (body: { extra: number; strategy: DebtOrder; account_ids: number[] }) =>
      request<{ state: DebtBudgetState; undo: { token: string } }>('PUT', '/api/debt/budget', body),
    /** 409 when it can't be undone any more. */
    undoBudget: (token: string) => request<{ state: DebtBudgetState }>('POST', '/api/debt/budget/undo', { token }),
    removeBudget: () => request<{ state: DebtBudgetState; undo: { token: string } }>('DELETE', '/api/debt/budget'),
  },
};

// ---------------------------------------------------------------- Release 3.10 pair 1 (Accounts + sidebar) types

/** A bank-connected account's connection (GET /api/accounts). */
export interface AccountConnection {
  item_id: number;
  kind: ItemKind;
  status: ItemStatus;
  /** ISO UTC; null before the first update. */
  last_synced_at: string | null;
}

/** Release 3.10 fields on GET /api/accounts (merged into `Account` above; optional so older fixtures still type-check). */
export interface Account {
  /** The bank's own name for the account (Plaid only; null until the first update after 3.10). `name` is the nickname. */
  bank_name?: string | null;
  /** null for manual accounts. */
  connection?: AccountConnection | null;
  /** Date of the newest recorded (not estimated) balance, "YYYY-MM-DD". */
  balance_date?: string | null;
  balance_age_days?: number | null;
  /** Manual, and the balance is 7 or more days old (30 for "other", like a house), or never saved. */
  stale?: boolean;
  /** The card's limit, when the bank sent one. */
  credit_limit?: number | null;
}

/** "What kind?" tiles in Add account (the backend maps each to a category and subtype). `other`: a house, a car. */
export type ManualKind = 'checking' | 'savings' | 'credit_card' | 'student' | 'auto' | 'other_loan' | 'investment' | 'other';

export interface NewAccountByKind {
  name: string;
  kind: ManualKind;
  /** Debts: the amount owed, positive. Checking, savings and other: negative when overdrawn. */
  current_balance: number;
  institution_name?: string;
  /** Exactly 4 digits. */
  mask?: string;
  interest_rate?: number;
  minimum_payment?: number;
}

export interface BalanceSaved {
  account: Account;
  undo: { token: string };
}

export interface AccountRemoved {
  undo: { token: string; name: string };
}

/** Home "Things that need you": a manual balance 7+ days old (fingerprint = balance_date or "none"). */
export type UpdateBalanceNeed = NeedBase<
  'update_balance',
  'warn',
  {
    account_id: number;
    name: string;
    mask: string | null;
    institution_name: string | null;
    /** Days since the balance was saved; null when it never was. */
    days: number | null;
    balance_date: string | null;
    account_category: Category;
  }
>;

/** GET /api/plaid/status: the lifetime count of bank connections made, against the plan's limit. */
export interface PlaidStatus {
  /** Iron Owl 2.0.0: counting against Plaid's Trial limit is on (a vault setting). */
  items_cap?: boolean;
  items_linked?: number;
  /** 10 while `items_cap` is on, 0 (no limit) while it's off. */
  items_limit?: number;
  /** The connections there are now: the lowest "used" count the owner can set. */
  items_now?: number;
}

/** PUT /api/plaid/items-linked and PUT /api/plaid/items-cap. */
export interface ConnectionsUsed {
  items_cap?: boolean;
  items_linked: number;
  items_limit: number;
  items_now: number;
}

// ---------------------------------------------------------------- Release 3.10 pair 2 (Reports tabs, Spending, Debt) types

/** GET /api/budgets: the "Paying off debt" category (Reports › Paying off debt › Add to my Budget). */
export interface EnvelopeLine {
  /** The extra a month the user set on Reports › Paying off debt; null for every other category. */
  debt?: { extra: number } | null;
}

/** GET /api/reports/spending?month=YYYY-MM (reports-spending design notes). */
export interface SpendingReport {
  today: string;
  /** Today's month. */
  current: string;
  /** The month shown. */
  month: string;
  first_data_date: string | null;
  /** The months the stepper can reach. */
  range: { first: string; last: string };
  /** The 6 months ending today's month. */
  months: { month: string; total: number; complete: boolean; has_data: boolean; partial: boolean }[];
  /** Full months with data (not partial) in the window; null before the first one. */
  average: { amount: number; months: string[] } | null;
  summary: {
    month: string;
    total: number;
    complete: boolean;
    partial: boolean;
    /** The month before (may be outside the window); null before the first month with data. */
    previous: { month: string; total: number; has_data: boolean; partial: boolean } | null;
  };
  categories: SpendingReportCategory[];
  /** Top 5, bills left out. */
  stores: { name: string; amount: number; count: number; category: { id: string; name: string; hue: number } | null }[];
  /** Category names left out of the stores list. */
  stores_left_out: string[];
}

export interface SpendingReportCategory {
  id: string;
  name: string;
  hue: number;
  kind: 'spending' | 'fixed';
  bill: boolean;
  /** In today's month's Budget. */
  in_budget: boolean;
  amount: number;
  /** The month before; null when there's no data for it. */
  previous: number | null;
  /** Whole percent of the month's total. */
  share: number;
  /** Newest first, refunds included (negative), at most 100. */
  purchases: { id: number; split: boolean; date: string; name: string; amount: number; pending: boolean }[];
  purchase_count: number;
  /** Purchases not listed. */
  more: number;
}

export type DebtKind = 'card' | 'loan' | 'mortgage';
/** Most interest first / smallest first. */
export type DebtOrder = 'avalanche' | 'snowball';

export interface DebtPlanRequest {
  extra: number;
  strategy: DebtOrder;
  account_ids?: number[];
  /** A one-time payment at the start: on `account_id`, or (null) in plan order. */
  lump?: { amount: number; account_id: number | null };
}

export interface DebtPlanDebt {
  account_id: number;
  name: string;
  kind: DebtKind;
  loan_group: string | null;
  balance: number;
  /** 0 when unknown (see apr_known). */
  apr: number;
  apr_known: boolean;
  minimum: number;
  payoff_months: number | null;
  payoff_date: string | null;
  interest: number;
  /** At today's minimum payments (no extra, no rollover). */
  baseline_months: number | null;
  baseline_date: string | null;
}

export interface DebtPlanV2 {
  strategy: DebtOrder | 'custom';
  extra: number;
  months: number | null;
  /** 'YYYY-MM-01'. */
  payoff_date: string | null;
  total_interest: number;
  never: boolean;
  /** Balance × rate ÷ 1200 today, summed (an estimate). */
  interest_this_month: number;
  /** The plan's interest over its first 12 months. */
  interest_12: number;
  /** In plan order (where the extra goes first). */
  debts: DebtPlanDebt[];
  timeline: { month: string; total: number; interest: number; by_account: Record<string, number> }[];
  compare: Record<'avalanche' | 'snowball' | 'minimums_only', { months: number | null; total_interest: number }>;
  /** At today's minimum payments. */
  baseline: { months: number | null; payoff_date: string | null; total_interest: number; never: boolean; timeline: { month: string; total: number }[] };
  /** Debts without a minimum payment (only when account_ids was left out). */
  skipped: { account_id: number; name: string; reason: string }[];
  /** The plan with the one-time payment (top-level numbers stay without it). */
  lump: {
    amount: number;
    account_id: number | null;
    applied: { account_id: number; amount: number }[];
    months: number | null;
    payoff_date: string | null;
    total_interest: number;
    never: boolean;
    /** Paid off by the payment right away. */
    paid_off: number[];
  } | null;
}

/** GET /api/debt/progress?ids=1,2 */
export interface DebtProgress {
  /** At most the last 12, ending today's month; only months where every included debt has a balance. */
  months: { month: string; total: number; estimated: boolean }[];
  since: string | null;
  /** Last − first; negative = went down. Null with fewer than 2 months. */
  change: number | null;
  /** Included debts whose history starts later (why the chart starts later). */
  missing: string[];
  /** Every included credit card; limit/used_pct null when the bank didn't share a limit. */
  cards: { account_id: number; name: string; balance: number; limit: number | null; used_pct: number | null }[];
}

/** GET /api/debt/budget */
export interface DebtBudgetState {
  budget_ready: boolean;
  linked: boolean;
  category_id: string | null;
  extra: number;
  strategy: DebtOrder;
  account_ids: number[];
  month: string;
  /** Null when the category isn't in this month's Budget. */
  planned_this_month: number | null;
  not_planned: number;
  /** The loans the Budget amount covers (loans only: card payoff is already part of their Budget money). */
  loans: { account_id: number; name: string }[];
}

/** PATCH /api/transactions (bulk) to the "Paying off debt" category: card payments are left out (also in `skipped`). */
export interface BulkCategoryResult {
  skipped_card_payments?: number[];
}
