import type { BudgetMonth, EnvelopeLine } from '../../api';

/** Round each API dollar value to cents before adding, just as Home's server totals do. */
const cents = (value: number): number => Math.round(value * 100);
const dollars = (value: number): number => value / 100;

type WorkspaceLine = Pick<EnvelopeLine, 'category' | 'assigned' | 'carryover' | 'spent' | 'available' | 'goal' | 'debt'>;

/** The same metadata scope as dashboard._spending_lines; category names never determine it. */
export function isReservedLine(line: Pick<WorkspaceLine, 'category' | 'goal' | 'debt'>, savingsId: string | null): boolean {
  return line.category === savingsId || line.goal != null || line.debt != null;
}

export function groupWorkspaceTotals(lines: WorkspaceLine[], savingsId: string | null) {
  let assigned = 0;
  let spent = 0;
  let funds = 0;
  let left = 0;
  let spendable = 0;
  let reserved = 0;
  let overCount = 0;
  for (const line of lines) {
    const plan = cents(line.assigned);
    // The existing savings row labels a negative assignment as "Used", rather than a
    // negative new plan. Only the group display compensates; actual funds stay signed.
    assigned += line.category === savingsId && plan < 0 ? 0 : plan;
    spent += cents(line.spent);
    funds += cents(line.carryover) + plan;
    const available = cents(line.available);
    left += available;
    if (isReservedLine(line, savingsId)) reserved += available;
    else spendable += available;
    if (available < 0) overCount += 1;
  }
  return {
    assigned: dollars(assigned), spent: dollars(spent), funds: dollars(funds),
    left: dollars(left), spendable: dollars(spendable), reserved: dollars(reserved), overCount,
  };
}

/** A group button: left and planned match the group card; the bar is used / this month's plan. */
export function groupSelectorStats(lines: WorkspaceLine[], savingsId: string | null) {
  if (!lines.length) return { kind: 'empty' as const };
  const reservedOnly = lines.every((line) => isReservedLine(line, savingsId));
  const active = lines.some((c) => Math.abs(c.assigned) > 0.004 || Math.abs(c.carryover) > 0.004 || Math.abs(c.spent) > 0.004);
  if (!reservedOnly && !active) return { kind: 'noplan' as const };
  const t = groupWorkspaceTotals(lines, savingsId);
  const planned = t.assigned > 0.004;
  return {
    kind: reservedOnly ? 'reserved' as const : 'spending' as const,
    left: reservedOnly ? t.reserved : t.left,
    planned: t.assigned,
    spent: t.spent,
    percent: planned ? Math.round((t.spent / t.assigned) * 100) : null,
    fill: planned ? Math.min(1, Math.max(0, t.spent / t.assigned)) : t.spent > 0.004 ? 1 : 0,
    overPlan: cents(t.spent) > cents(t.assigned),
    over: (reservedOnly ? t.reserved : t.left) < -0.004,
  };
}

/** Spending is the envelope scope used by Home. Unbudgeted/fixed spending remains outside
 * this subtotal and retains its server-provided effect on ready_to_assign. */
export function budgetWorkspaceTotals(bm: Pick<BudgetMonth, 'categories' | 'savings_category' | 'ready_to_assign'>) {
  let assigned = 0;
  let carryover = 0;
  let spent = 0;
  let left = 0;
  let reserved = 0;
  for (const line of bm.categories) {
    if (isReservedLine(line, bm.savings_category)) {
      reserved += cents(line.available);
      continue;
    }
    assigned += cents(line.assigned);
    carryover += cents(line.carryover);
    spent += cents(line.spent);
    // Use the authoritative envelope balance, including any existing server treatment.
    left += cents(line.available);
  }
  const unplanned = cents(bm.ready_to_assign);
  return {
    spendingAssigned: dollars(assigned), spendingCarryover: dollars(carryover),
    spent: dollars(spent), left: dollars(left), reserved: dollars(reserved),
    unplanned: dollars(unplanned), overPlanned: dollars(Math.max(0, -unplanned)),
  };
}

/** A typed plan: "$1,250.5" -> 1250.5; anything else (including negatives) -> null. */
export function parsePlan(s: string): number | null {
  const t = s.replace(/[$,\s]/g, '');
  if (!/^(\d+\.?\d*|\.\d+)$/.test(t)) return null;
  const v = Number(t);
  return Number.isFinite(v) ? dollars(cents(v)) : null;
}

export interface PlanHint { text: string; warn: boolean; blocksSave: boolean }

/** Live preview under the panel's plan input; changePlan's answer still decides on save. */
export function planHint(
  { draft, assigned, readyToAssign, monthName, monthMoney }: { draft: string; assigned: number; readyToAssign: number; monthName: string; monthMoney: number },
  money: (value: number) => string,
): PlanHint | null {
  if (!draft.trim()) return null;
  const value = parsePlan(draft);
  if (value === null) return { text: 'Type a planned amount, like 650.', warn: true, blocksSave: false };
  const change = cents(value) - cents(assigned);
  if (change === 0) return null;
  const notPlanned = Math.max(0, cents(readyToAssign));
  if (change > notPlanned) {
    return { text: `That would plan ${money(dollars(change - cents(readyToAssign)))} more than ${monthName}’s ${money(monthMoney)}. Lower another plan first, or change the month’s amount.`, warn: true, blocksSave: true };
  }
  if (change > 0) return { text: `This takes ${money(dollars(change))} from Not planned yet (${money(dollars(notPlanned))} left).`, warn: false, blocksSave: false };
  return { text: `${money(dollars(-change))} goes back to Not planned yet.`, warn: false, blocksSave: false };
}

export function parseAllocationAmount(draft: string): number | null {
  const value = draft.replace(/[$,\s]/g, '');
  if (!/^(\d+(\.\d{0,2})?|\.\d{1,2})$/.test(value)) return null;
  const amount = Number(value);
  const amountCents = cents(amount);
  return Number.isFinite(amount) && Number.isSafeInteger(amountCents) && amountCents > 0 ? dollars(amountCents) : null;
}

/** Preview an addition using the latest envelope balance, including its carryover/refunds. */
export function allocationPreview(line: Pick<WorkspaceLine, 'assigned' | 'available'>, amount: number, unplanned: number) {
  return {
    planned: dollars(cents(line.assigned) + cents(amount)),
    left: dollars(cents(line.available) + cents(amount)),
    unplanned: dollars(cents(unplanned) - cents(amount)),
  };
}

/**
 * Release 3.17: the save for a ONE-TIME move (Move money, Cover it). Plans repeat each month but
 * moves don't, so each side sends its new total in `assigned` and this month's one-time moves in
 * `moved` (its current `moved` plus the change): the plan that repeats next month stays the same.
 * `changes`: category id → dollars in (+) or out (−). Null when a category isn't in `lines`.
 */
export function oneTimeMove(
  lines: readonly Pick<EnvelopeLine, 'category' | 'assigned' | 'moved'>[],
  changes: Record<string, number>,
): { assigned: Record<string, number>; moved: Record<string, number> } | null {
  const assigned: Record<string, number> = {};
  const moved: Record<string, number> = {};
  for (const [id, change] of Object.entries(changes)) {
    const line = lines.find((c) => c.category === id);
    if (!line) return null;
    assigned[id] = dollars(cents(line.assigned) + cents(change));
    moved[id] = dollars(cents(line.moved ?? 0) + cents(change));
  }
  return { assigned, moved };
}
