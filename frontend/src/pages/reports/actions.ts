/**
 * Reports v2 action buttons (SPEC "Reports v2 ... (Release 3.14)", Action buttons): plan next
 * month's amount for a category, move money to savings this month, and turn on the Budget alert.
 * Each returns the toast words and an Undo that puts the previous value back through the same
 * save path. The api is passed in, and nothing here imports at runtime, so
 * `npm run test:unit` (scripts/reportsOverview.test.ts) can load it with a stub.
 * The React side (toast, busy state) is `useReportActions.tsx`.
 */
import type { AlertKey, AlertSetting, BudgetMonth, BudgetRowRestore, BudgetRowState, BudgetSave } from '../../api';

/** The parts of `api` the actions use (a stub in tests). */
export interface ActionsApi {
  budgets: {
    get: (month?: string) => Promise<BudgetMonth>;
    save: (month: string, body: BudgetSave) => Promise<BudgetMonth>;
    /** One budgets row as it is (state null: no row). */
    rowState: (month: string, category: string) => Promise<{ state: BudgetRowState | null }>;
    /**
     * Put rows back exactly (state null: delete the row), all or nothing: refused (409) when any
     * row is no longer `expected` (changed since), or (422) when it breaks a budget rule.
     */
    putBackRows: (month: string, rows: BudgetRowRestore[]) => Promise<unknown>;
  };
  alerts: {
    settings: () => Promise<AlertSetting[]>;
    updateSetting: (key: AlertKey, patch: { enabled?: boolean; value?: number }) => Promise<AlertSetting>;
  };
}

/** What an action did: the toast words and its Undo (resolves null when undone, else the error words). */
export type ActionOutcome = { ok: true; message: string; undo: () => Promise<string | null> } | { ok: false; error: string };

export interface CategoryRef {
  id: string;
  name: string;
}

// ---------------------------------------------------------------- small helpers (no imports)

const fmt0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
const fmt2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
/** "$280", or "$47.50" when there are cents. */
export function actionMoney(v: number): string {
  const a = Math.abs(v);
  return Math.abs(a - Math.round(a)) < 0.005 ? fmt0.format(Math.round(a)) : fmt2.format(a);
}
const round2 = (v: number) => Math.round(v * 100) / 100;

/** "2026-09" → "2026-10". */
export function nextMonthKey(month: string): string {
  const [y, m] = month.split('-').map(Number) as [number, number];
  const d = new Date(y, m, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}
/** "2026-10" → "October". */
export function monthWord(month: string): string {
  const [y, m] = month.split('-').map(Number) as [number, number];
  return new Date(y, m - 1, 1).toLocaleString('en-US', { month: 'long' });
}

function errText(err: unknown): string {
  // ApiError carries the server's own words in `detail`; `status 0` means the app didn't answer.
  if (err && typeof err === 'object') {
    const e = err as { status?: unknown; detail?: unknown; message?: unknown };
    if (e.status === 0) return 'Iron Owl isn’t responding. Please wait a moment and try again.';
    if (typeof e.detail === 'string' && e.detail) return e.detail;
    if (typeof e.message === 'string' && e.message) return e.message;
  }
  return 'Something went wrong.';
}

// ---------------------------------------------------------------- exact Undo for budget saves

const NO_UNDO = 'This change can’t be undone here. You can change it on the Budget page.';

/**
 * Save `body` to `month` and build its Undo: the touched categories' rows are read before and
 * right after the save, and Undo puts them all back in one all-or-nothing call that the server
 * refuses (with its own words, nothing changed) when any row was changed since.
 */
async function saveWithUndo(api: ActionsApi, month: string, categories: string[], body: BudgetSave): Promise<() => Promise<string | null>> {
  const before = await Promise.all(categories.map(async (c) => (await api.budgets.rowState(month, c)).state));
  await api.budgets.save(month, body);
  let after: (BudgetRowState | null)[];
  try {
    after = await Promise.all(categories.map(async (c) => (await api.budgets.rowState(month, c)).state));
  } catch {
    // Saved, but without the row as it is now an exact Undo isn't safe.
    return async () => NO_UNDO;
  }
  const rows: BudgetRowRestore[] = categories.map((category, i) => ({ category, state: before[i]!, expected: after[i]! }));
  return async () => {
    try {
      await api.budgets.putBackRows(month, rows);
      return null;
    } catch (err) {
      return errText(err);
    }
  };
}

// ---------------------------------------------------------------- Plan $X for {next month}

/** The amount "Plan $X" offers: the pace rounded to the nearest $10 (at least $10). */
export function planAmountFor(pace: number): number {
  return Math.max(10, Math.round(pace / 10) * 10);
}

/**
 * Plan `amount` for the category in the month after `month` (Overview "Plan $X for October",
 * Habits "Set the cap"). Refused, with Budget's over-plan words, when next month's money can't
 * cover the increase; the server's refusal is shown as it is. The save writes only next
 * month's row; Undo puts that row back exactly as it was (no row, a removed row, or the earlier
 * amount), so a category that wasn't in the plan doesn't stay behind at $0.
 */
export async function planNextMonth(api: ActionsApi, month: string, cat: CategoryRef, amount: number): Promise<ActionOutcome> {
  const next = nextMonthKey(month);
  const value = round2(amount);
  if (!Number.isFinite(value) || value < 0) return { ok: false, error: 'That amount doesn’t work. Try again.' };
  let undo: () => Promise<string | null> = async () => null;
  try {
    const bm = await api.budgets.get(next);
    const extra = round2(value - (bm.categories.find((c) => c.category === cat.id)?.assigned ?? 0));
    const inPlan = bm.categories.some((c) => c.category === cat.id);
    if (extra > Math.max(0, bm.ready_to_assign) + 0.004) {
      const over = round2(extra - bm.ready_to_assign);
      return {
        ok: false,
        error: `That would plan ${actionMoney(over)} more than ${monthWord(next)}’s ${actionMoney(bm.month_money)}. Lower another plan first, or change the month’s amount.`,
      };
    }
    if (!inPlan || Math.abs(extra) >= 0.005) undo = await saveWithUndo(api, next, [cat.id], { assigned: { [cat.id]: value } });
  } catch (err) {
    return { ok: false, error: errText(err) };
  }
  return {
    ok: true,
    message: `${monthWord(next)}’s plan for ${cat.name} is now ${actionMoney(value)}.`,
    undo,
  };
}

// ---------------------------------------------------------------- Move $X to savings

/**
 * How much "Move $X to savings" can move from the category this month: the wanted amount in
 * whole dollars, capped at what the category has left (and at what Budget's Move money allows).
 * 0 hides the button: no savings category, the category is the savings one or isn't in the plan,
 * or nothing is left.
 */
export function movableToSavings(bm: BudgetMonth | null | undefined, categoryId: string, wanted: number): number {
  if (!bm || !bm.savings_category || bm.savings_category === categoryId) return 0;
  const line = bm.categories.find((c) => c.category === categoryId);
  if (!line || !bm.categories.some((c) => c.category === bm.savings_category)) return 0;
  const cap = Math.min(line.available, line.carryover + line.assigned);
  const amt = Math.floor(Math.min(wanted, cap) + 0.005);
  return amt >= 1 ? amt : 0;
}

/**
 * Move `amount` from the category to the savings category this month (`month` = the current
 * month). Re-reads the budget first, so the amount is checked against the latest numbers.
 * Undo puts both rows back exactly, together or not at all (refused when either changed since).
 */
export async function moveToSavings(api: ActionsApi, month: string, cat: CategoryRef, amount: number): Promise<ActionOutcome> {
  let savingsName: string;
  let undo: () => Promise<string | null>;
  try {
    const bm = await api.budgets.get(month);
    const can = movableToSavings(bm, cat.id, amount);
    if (!bm.savings_category) return { ok: false, error: 'There’s no savings category to move money to. Choose one in Budget.' };
    if (can < amount - 0.004) {
      return { ok: false, error: can < 1 ? `${cat.name} has nothing left to move.` : `You can move at most ${actionMoney(can)} out of ${cat.name}. Use a smaller amount.` };
    }
    const savingsId = bm.savings_category;
    const f = bm.categories.find((c) => c.category === cat.id)!;
    const s = bm.categories.find((c) => c.category === savingsId)!;
    savingsName = s.name;
    // One-time (Release 3.17): `moved` keeps both plans that repeat next month as they were.
    undo = await saveWithUndo(api, month, [cat.id, savingsId], {
      assigned: { [cat.id]: round2(f.assigned - amount), [savingsId]: round2(s.assigned + amount) },
      moved: { [cat.id]: round2((f.moved ?? 0) - amount), [savingsId]: round2((s.moved ?? 0) + amount) },
    });
  } catch (err) {
    return { ok: false, error: errText(err) };
  }
  return {
    ok: true,
    message: `Moved ${actionMoney(amount)} from ${cat.name} to ${savingsName}.`,
    undo,
  };
}

// ---------------------------------------------------------------- Remind me (Budget alert)

/** Is the all-categories Budget alert on? null when the list doesn't have it. */
export function budgetAlertOn(settings: AlertSetting[] | null | undefined): boolean | null {
  const s = settings?.find((x) => x.key === 'budget');
  return s ? s.enabled : null;
}

export const BUDGET_ALERT_ON_MESSAGE = 'Budget alerts are on. You’ll get a note when a category goes over its plan.';

/** Turn on the existing Budget alert (SPEC: all categories, no per-category or 80% reminder). Undo turns it off again. */
export async function turnOnBudgetAlert(api: ActionsApi): Promise<ActionOutcome> {
  try {
    await api.alerts.updateSetting('budget', { enabled: true });
  } catch (err) {
    return { ok: false, error: errText(err) };
  }
  return {
    ok: true,
    message: BUDGET_ALERT_ON_MESSAGE,
    undo: async () => {
      try {
        await api.alerts.updateSetting('budget', { enabled: false });
        return null;
      } catch (err) {
        return errText(err);
      }
    },
  };
}
