/**
 * DEV-ONLY: Goals (design D8, SPEC "Goals (D8) and Investments (D9) redesign"). `&goals=<state>`:
 *   ontrack   (default) Emergency fund $2,450 of $6,000 at $400; Holiday gifts and New tires on track
 *   behind    Holiday gifts at $50 a month: $50 short (the amber box, "Set aside $65")
 *   reached   New tires has its $640 ("It's ready", I spent it / Raise the goal)
 *   empty     no goals yet ("Start with one goal")
 *   old       plus an old savings goal not in the Budget yet ("Not in your Budget yet")
 *   nobudget  the Budget isn't set up ("Set up your Budget first →"), one old goal
 * With a Budget scenario (&budget=normal&goals=…, see mockBudget) the goals are categories in
 * that budget: the Budget page shows them ("Goal · $X of $Y saved") and the Goals page uses its
 * Not planned yet. Without one, the numbers are the design's: September's $4,800 with $3,435
 * planned elsewhere. Same rules as the backend (plan = min(monthly, target − saved), refused
 * above Not planned yet with code `not_enough_unplanned`).
 */
import type { Goal, GoalKind, GoalsState, GoalUndo } from '../api';
import { json, state } from './mock';
import { budgetBridge, type BudgetCat } from './mockBudget';

type Scenario = 'ontrack' | 'behind' | 'reached' | 'empty' | 'old' | 'nobudget';
const SCENARIOS: Scenario[] = ['ontrack', 'behind', 'reached', 'empty', 'old', 'nobudget'];
const qs = new URLSearchParams(window.location.search);
const raw = qs.get('goals');
const scenario: Scenario = SCENARIOS.includes(raw as Scenario) ? (raw as Scenario) : 'ontrack';
/** Goals live in the Budget scenario's categories only when both are asked for. */
const linked = () => budgetBridge.active() && raw !== null;

const round = (n: number) => Math.round(n * 100) / 100;
const usd = (n: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: Number.isInteger(round(n)) ? 0 : 2 }).format(n);
const pad = (n: number) => String(n).padStart(2, '0');
const monthKey = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const shift = (m: string, n: number) => {
  const [y, mo] = m.split('-').map(Number);
  return monthKey(new Date(y!, mo! - 1 + n, 1, 12));
};
const diff = (a: string, b: string) => {
  const [ay, am] = a.split('-').map(Number);
  const [by, bm] = b.split('-').map(Number);
  return (by! - ay!) * 12 + (bm! - am!);
};
const T = monthKey(new Date());
const INCOME = 4800;
const OTHER_PLANNED = 3435;

interface G {
  id: number;
  kind: GoalKind;
  name: string;
  target: number;
  due: string | null;
  monthly: number;
  account_id: number | null;
  in_budget: boolean;
  /** Linked mode: the budget category. */
  cat: string | null;
  /** Standalone mode: what's saved (the envelope without this month's plan). */
  saved: number;
}

let goals: G[] | null = null;
let nextId = 50;
/** Standalone mode: money "Already saved" took out of Not planned yet. */
let seedsTaken = 0;
/** Deletes that Undo can put back once, by token. */
const deleted = new Map<string, { g: G; cat: BudgetCat | null; prior?: { cat: string; name: string; plan: number; carry: number } }>();
let nextToken = 1;
/** Emergency funds that took over Emergency savings: its name and money before (the add's Undo puts them back). */
const takenOver = new Map<number, { cat: string; name: string; plan: number; carry: number }>();

const planFor = (monthly: number, target: number, saved: number) => round(Math.min(Math.max(0, monthly), Math.max(0, target - saved)));
const savedOf = (g: G) => (linked() && g.cat ? (budgetBridge.cat(g.cat)?.carry ?? 0) : g.saved);
const plannedOf = (g: G) => (!g.in_budget ? 0 : linked() && g.cat ? (budgetBridge.cat(g.cat)?.plan ?? 0) : planFor(g.monthly, g.target, g.saved));
/** Like the server: a lower plan only takes back this month's own plan, never more than the envelope has. */
const lowestOf = (g: G) => {
  const plan = plannedOf(g);
  return round(plan - Math.min(Math.max(0, plan), Math.max(0, savedOf(g) + plan)));
};

/** Seeds the scenario (and, with a Budget scenario, its categories) on first use. */
export function ensureGoals(): G[] {
  if (goals) return goals;
  const base: G[] = [
    { id: 1, kind: 'emergency', name: 'Emergency fund', target: 6000, due: null, monthly: 400, account_id: 2, in_budget: true, cat: null, saved: 2450 },
    { id: 2, kind: 'save', name: 'Holiday gifts', target: 600, due: shift(T, 3), monthly: 90, account_id: 2, in_budget: true, cat: null, saved: 350 },
    { id: 3, kind: 'save', name: 'New tires', target: 640, due: shift(T, 6), monthly: 100, account_id: 2, in_budget: true, cat: null, saved: 120 },
  ];
  const old: G = { id: 9, kind: 'save', name: 'Japan trip', target: 6000, due: shift(T, 8), monthly: 400, account_id: null, in_budget: false, cat: null, saved: 0 };
  if (scenario === 'behind') base[1]!.monthly = 50;
  if (scenario === 'reached') base[2]!.saved = 640;
  goals = scenario === 'empty' ? [] : scenario === 'old' ? [...base, old] : scenario === 'nobudget' ? [old] : base;
  if (linked() && budgetBridge.ready()) {
    for (const g of goals) {
      if (!g.in_budget) continue;
      const plan = planFor(g.monthly, g.target, g.saved);
      if (g.kind === 'emergency' && budgetBridge.savingsId()) {
        // The Emergency fund takes over Emergency savings (its money is what's there).
        g.cat = budgetBridge.savingsId();
        budgetBridge.set(g.cat!, { name: g.name, plan: planFor(g.monthly, g.target, budgetBridge.cat(g.cat!)?.carry ?? 0) });
      } else {
        g.cat = budgetBridge.addCat(g.name, plan, g.saved, { money: true });
      }
    }
  }
  return goals;
}

/** EnvelopeLine.goal for a budget category. */
export function goalInfo(catId: string): { id: number; kind: GoalKind; saved: number; target: number; reached: boolean; plan: number } | null {
  if (!linked()) return null;
  const g = ensureGoals().find((x) => x.cat === catId);
  if (!g) return null;
  const saved = savedOf(g);
  return { id: g.id, kind: g.kind, saved, target: g.target, reached: saved >= g.target - 0.004, plan: planFor(g.monthly, g.target, saved) };
}

function acctLabel(id: number | null): string | null {
  const a = id === null ? undefined : state.accounts.find((x) => x.id === id);
  return a ? `${a.name}${a.mask ? ` ··${a.mask}` : ''}` : null;
}

function notPlanned(): number {
  if (linked()) return round(budgetBridge.notPlanned());
  return round(INCOME - OTHER_PLANNED - ensureGoals().reduce((s, g) => s + plannedOf(g), 0) - seedsTaken);
}

function derive(g: G): Goal {
  const saved = round(savedOf(g));
  const planned = round(plannedOf(g));
  const reached = saved >= g.target - 0.004;
  const months = g.kind === 'save' && g.due ? diff(T, g.due) + 1 : null;
  const projected = g.kind === 'save' && months !== null ? round(Math.min(g.target, saved + g.monthly * Math.max(0, months))) : saved;
  let status: Goal['status'] = 'on_track';
  if (reached) status = 'reached';
  else if (g.monthly < 0.005) status = 'no_plan';
  else if (g.kind === 'save' && projected < g.target - 0.004) status = 'behind';
  const left = Math.max(0, g.target - saved);
  const projected_month = g.kind === 'emergency' && g.monthly > 0.004 && !reached ? shift(T, Math.ceil(left / g.monthly - 1e-9) - 1) : null;
  const needed = g.kind === 'save' && months !== null && months >= 1 && !reached ? Math.ceil(Math.ceil(left / months) / 5) * 5 : null;
  return {
    id: g.id, kind: g.kind, name: g.name, category_id: g.cat, in_budget: g.in_budget, target: g.target, due_month: g.due, monthly: g.monthly,
    planned_this_month: planned, lowest_plan: g.in_budget ? lowestOf(g) : null, saved: g.in_budget ? saved : 0, account_id: g.account_id, account_label: acctLabel(g.account_id), status,
    months_to_save: months, projected, projected_month, short_by: status === 'behind' ? round(g.target - projected) : null, needed_monthly: needed,
  };
}

function goalsState(): GoalsState {
  const list = ensureGoals();
  const ready = linked() ? budgetBridge.ready() : scenario !== 'nobudget';
  const ef = list.find((g) => g.kind === 'emergency' && g.in_budget);
  const savingsCat = linked() ? budgetBridge.savingsId() : null;
  return {
    month: T,
    month_money: linked() ? budgetBridge.monthMoney() : INCOME,
    not_planned: notPlanned(),
    monthly_total: round(list.reduce((s, g) => s + plannedOf(g), 0)),
    budget_ready: ready,
    emergency_suggestion: ef ? null : 5600,
    emergency_available: ef ? null : linked() ? (savingsCat ? (budgetBridge.cat(savingsCat)?.carry ?? 0) : 0) : 1200,
    emergency_basis: ef ? null : 'bills',
    emergency_monthly: ef ? null : 1860.99,
    goals: list.map(derive),
  };
}

const refuse = (np: number, extra: Record<string, unknown> = {}, detail?: string) =>
  json(422, {
    detail: detail ?? `Only ${usd(Math.max(0, np))} isn't planned yet this month. Use a smaller amount, or take money from another category on the Budget page.`,
    code: 'not_enough_unplanned',
    not_planned: Math.max(0, round(np)),
    ...extra,
  });

const ALLOWED_POST = ['kind', 'name', 'target', 'due_month', 'monthly', 'already_saved', 'account_id'];
const ALLOWED_PATCH = ['name', 'target', 'due_month', 'monthly', 'account_id', 'already_saved', 'plan_before'];
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

function checkDue(kind: GoalKind, due: unknown): string | null {
  if (kind === 'emergency') return due === null || due === undefined ? null : 'An emergency fund has no date.';
  if (typeof due !== 'string' || !/^\d{4}-\d{2}$/.test(due)) return 'Pick the month you need it by.';
  const d = diff(T, due);
  return d < 1 || d > 24 ? 'Pick a month in the next two years.' : null;
}

/** "Already saved": what's left of Not planned yet after the plan, then the emergency fund. */
function takeSeed(already: number, npAfterPlan: number, selfIsEmergency: boolean): Response | { fromEf: number } {
  const fromUnplanned = Math.min(already, Math.max(0, npAfterPlan));
  const fromEf = round(already - fromUnplanned);
  if (fromEf < 0.005) return { fromEf: 0 };
  const ef = selfIsEmergency ? undefined : ensureGoals().find((g) => g.kind === 'emergency' && g.in_budget);
  const efMoney = ef ? round(savedOf(ef) + plannedOf(ef)) : 0;
  if (fromEf > efMoney + 0.004) {
    return refuse(npAfterPlan, { emergency_available: efMoney }, `Only ${usd(Math.max(0, npAfterPlan))} isn't planned yet${efMoney > 0 ? ` and your emergency fund has ${usd(efMoney)}` : ''}. Use a smaller amount for Already saved.`);
  }
  if (ef) {
    if (linked() && ef.cat) {
      const c = budgetBridge.cat(ef.cat)!;
      budgetBridge.set(ef.cat, { plan: round(c.plan - fromEf) });
    } else ef.saved = round(ef.saved - fromEf);
  }
  return { fromEf };
}

function create(body: Record<string, unknown>): Response {
  const extra = Object.keys(body).filter((k) => !ALLOWED_POST.includes(k));
  if (extra.length) return json(422, { detail: `Unknown field: ${extra[0]}` });
  const ready = linked() ? budgetBridge.ready() : scenario !== 'nobudget';
  if (!ready) return json(409, { detail: 'Set up your Budget first.' });
  const kind = body.kind === 'emergency' ? 'emergency' : body.kind === 'save' ? 'save' : null;
  if (!kind) return json(422, { detail: 'Pick the kind of goal.' });
  const name = String(body.name ?? '').trim().replace(/\s+/g, ' ');
  if (!name || name.length > 60) return json(422, { detail: 'Give the goal a name (up to 60 letters).' });
  if (!finite(body.target) || body.target <= 0) return json(422, { detail: 'The goal amount must be more than $0.' });
  if (!finite(body.monthly) || body.monthly < 0) return json(422, { detail: 'The monthly amount can’t be below $0.' });
  const already = body.already_saved === undefined ? 0 : body.already_saved;
  if (!finite(already) || already < 0) return json(422, { detail: 'Already saved can’t be below $0.' });
  const dueErr = checkDue(kind, body.due_month);
  if (dueErr) return json(422, { detail: dueErr });
  const list = ensureGoals();
  if (kind === 'emergency' && list.some((g) => g.kind === 'emergency')) return json(409, { detail: 'You already have an emergency fund.' });
  if (list.some((g) => g.name.toLowerCase() === name.toLowerCase())) return json(409, { detail: `A category named “${name}” already exists.` });
  const savingsCat = linked() ? budgetBridge.savingsId() : null;
  const takeOver = kind === 'emergency' && savingsCat ? (budgetBridge.cat(savingsCat)?.carry ?? 0) : kind === 'emergency' && !linked() ? 1200 : 0;
  const saved = round(takeOver + already);
  const plan = planFor(body.monthly, body.target, saved);
  // With a takeover, Emergency savings' own plan is part of what's planned already.
  const ownPlan = kind === 'emergency' && savingsCat ? (budgetBridge.cat(savingsCat)?.plan ?? 0) : 0;
  const np = notPlanned() + ownPlan;
  if (plan > np + 0.004) return refuse(np);
  const seed = takeSeed(already, round(np - plan), kind === 'emergency');
  if (seed instanceof Response) return seed;
  const g: G = {
    id: nextId++, kind, name, target: round(body.target), due: kind === 'save' ? (body.due_month as string) : null, monthly: round(body.monthly),
    account_id: typeof body.account_id === 'number' ? body.account_id : null, in_budget: true, cat: null, saved,
  };
  if (linked()) {
    if (kind === 'emergency' && savingsCat) {
      const was = budgetBridge.cat(savingsCat);
      if (was) takenOver.set(g.id, { cat: savingsCat, name: was.name, plan: was.plan, carry: was.carry });
      g.cat = savingsCat;
      budgetBridge.set(savingsCat, { name, plan, carry: saved });
    } else g.cat = budgetBridge.addCat(name, plan, already, { seed: already > 0 ? already : undefined });
  } else seedsTaken = round(seedsTaken + already - seed.fromEf);
  list.push(g);
  return json(201, { ...goalsState(), goal_id: g.id });
}

function patch(g: G, body: Record<string, unknown>): Response {
  const extra = Object.keys(body).filter((k) => !ALLOWED_PATCH.includes(k));
  if (extra.length) return json(422, { detail: `Unknown field: ${extra[0]}` });
  const linking = !g.in_budget;
  if ('already_saved' in body && !linking) return json(422, { detail: 'Already saved can only be set when the goal joins your Budget.' });
  const next = { ...g };
  if ('name' in body) {
    const name = String(body.name ?? '').trim().replace(/\s+/g, ' ');
    if (!name || name.length > 60) return json(422, { detail: 'Give the goal a name (up to 60 letters).' });
    if (ensureGoals().some((x) => x !== g && x.name.toLowerCase() === name.toLowerCase())) return json(409, { detail: `A category named “${name}” already exists.` });
    next.name = name;
  }
  if ('target' in body) {
    if (!finite(body.target) || body.target <= 0) return json(422, { detail: 'The goal amount must be more than $0.' });
    next.target = round(body.target);
  }
  if ('monthly' in body) {
    if (!finite(body.monthly) || body.monthly < 0) return json(422, { detail: 'The monthly amount can’t be below $0.' });
    next.monthly = round(body.monthly);
  }
  if ('due_month' in body && body.due_month !== g.due) {
    const dueErr = checkDue(g.kind, body.due_month);
    if (dueErr) return json(422, { detail: dueErr });
    next.due = g.kind === 'save' ? (body.due_month as string) : null;
  }
  if ('account_id' in body) next.account_id = typeof body.account_id === 'number' ? body.account_id : null;
  const already = linking && finite(body.already_saved) ? Math.max(0, body.already_saved) : 0;
  const saved = linking ? already : savedOf(g);
  // Like the server: a goal in the Budget changes its plan by the difference between the new and
  // the old rule's plan (money moved in or out on the Budget page stays); one joining takes it all.
  const rulePlan = planFor(g.monthly, g.target, saved);
  const newRule = planFor(next.monthly, next.target, saved);
  let plan = linking ? newRule : round(plannedOf(g) + newRule - rulePlan);
  if (!linking && newRule < rulePlan) plan = Math.max(plan, lowestOf(g));
  const before = body.plan_before as { month?: unknown; planned?: unknown } | undefined;
  if (before && typeof before === 'object') {
    if (before.month !== T) return json(409, { detail: "This can't be undone any more: a new month has started." });
    if (!finite(before.planned)) return json(422, { detail: 'plan_before.planned must be a number' });
    if (!linking) plan = round(before.planned);
  }
  const np = notPlanned();
  if ((linking ? plan : plan - plannedOf(g)) > np + 0.004) return refuse(np);
  if (linking) {
    const seed = takeSeed(already, round(np - plan), g.kind === 'emergency');
    if (seed instanceof Response) return seed;
    next.in_budget = true;
    next.saved = already;
    if (linked()) next.cat = budgetBridge.addCat(next.name, plan, already, { seed: already > 0 ? already : undefined });
    else seedsTaken = round(seedsTaken + already - seed.fromEf);
  } else if (linked() && g.cat) {
    budgetBridge.set(g.cat, { name: next.name, plan });
  }
  Object.assign(g, next);
  return json(200, goalsState());
}

function remove(g: G, reason: string | null): Response {
  if (reason !== 'deleted' && reason !== 'spent' && reason !== 'undo_add') return json(422, { detail: 'reason must be deleted, spent or undo_add' });
  const list = ensureGoals();
  const prior = takenOver.get(g.id);
  if (reason === 'undo_add') {
    // The add's Undo: a taken-over Emergency savings goes back exactly as it was.
    const np0 = notPlanned();
    if (prior) budgetBridge.set(prior.cat, { name: prior.name, plan: prior.plan, carry: prior.carry });
    else if (linked() && g.cat) budgetBridge.remove(g.cat);
    takenOver.delete(g.id);
    goals = list.filter((x) => x !== g);
    return json(200, { state: goalsState(), kept_in: prior ? prior.name : null, released: round(notPlanned() - np0) });
  }
  const saved = savedOf(g);
  const planned = plannedOf(g);
  const token = `t${nextToken++}`;
  // A taken-over Emergency savings is handed back (its name; it keeps its money); a goal's own category leaves.
  const cat = linked() && g.cat && !prior ? budgetBridge.remove(g.cat) : null;
  if (prior) budgetBridge.set(prior.cat, { name: prior.name });
  takenOver.delete(g.id);
  deleted.set(token, { g: { ...g }, cat, prior });
  goals = list.filter((x) => x !== g);
  const undo: GoalUndo = {
    goal: { kind: g.kind, name: g.name, target: g.target, due_month: g.due, monthly: g.monthly, account_id: g.account_id },
    category_id: g.cat,
    month: T,
    assigned: planned,
    released: g.in_budget && !prior ? round(saved + planned) : 0,
    seed_month: null,
    removed: g.in_budget && !prior,
    token,
  };
  return json(200, { state: goalsState(), undo, kept_in: prior ? prior.name : null });
}

function restore(body: Record<string, unknown>): Response {
  const u = body as unknown as GoalUndo;
  if (u.month !== T) return json(409, { detail: 'The month changed, so this can’t be put back.' });
  const hit = typeof u.token === 'string' ? deleted.get(u.token) : undefined;
  if (!hit || hit.g.cat !== u.category_id) return json(409, { detail: 'This can’t be undone any more.' });
  const { g, cat, prior } = hit;
  deleted.delete(u.token);
  if (cat) budgetBridge.restore(cat);
  if (prior) {
    budgetBridge.set(prior.cat, { name: g.name });
    takenOver.set(g.id, prior);
  }
  ensureGoals().push(g);
  ensureGoals().sort((a, b) => a.id - b.id);
  return json(201, goalsState());
}

export async function handleGoals(method: string, url: URL, body: Record<string, unknown>): Promise<Response | null> {
  const path = url.pathname;
  if (path === '/api/goals' && method === 'GET') return json(200, goalsState());
  if (path === '/api/goals' && method === 'POST') return create(body);
  if (path === '/api/goals/restore' && method === 'POST') return restore(body);
  const m = /^\/api\/goals\/(\d+)$/.exec(path);
  if (m) {
    const g = ensureGoals().find((x) => x.id === Number(m[1]));
    if (!g) return json(404, { detail: 'Goal not found.' });
    if (method === 'PATCH') return patch(g, body);
    if (method === 'DELETE') return remove(g, url.searchParams.get('reason'));
  }
  return null;
}
