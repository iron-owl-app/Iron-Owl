import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, errorMessage, type Account, type BudgetIncomeMode, type BudgetMonth, type BudgetSave, type BudgetSetupInput, type CategoryGroupSnapshot, type CategoryPatch, type CategorySnapshot, type CategoryTarget, type EnvelopeLine, type RecurringInput, type SpendLine, type TxnCategory } from '../../api';
import { ErrorPanel } from '../../components/ErrorPanel';
import { SkeletonRows } from '../../components/ui';
import { useToast } from '../../components/Toast';
import { UndoToast } from '../../components/UndoToast';
import { Modal } from '../../components/Modal';
import { Icon } from '../../components/Icon';
import { readPref, writePref } from '../../lib/prefs';
import { formatMoney, plural, toISODate } from '../../lib/format';
import { addMonths, moneyShort, monthLong, monthName, monthOf, round2, targetOf } from '../../lib/budget';
import { AddItemDialog } from '../recurring/AddItemDialog';
import { groupHueAt, UNGROUPED_HUE } from '../../lib/categoryColors';
import { useBudgetMonth, type Snapshot } from './useBudgetMonth';
import { AddGroupForm, GroupCard, type RowActions, type Section } from './GroupCard';
import { HelpCard, IncomeNote, NotInPlanCard, type MoreTile } from './SimpleParts';
import { oneTimeMove } from './workspaceMath';
import { GroupSelector } from './GroupSelector';
import { BudgetOverview, BudgetPlanningStatus } from './BudgetOverview';
import { BudgetAllocationModal } from './BudgetAllocationModal';
import { SetupCard } from './SetupCard';
import { CoverPicker, IncomeSettingsModal, MoveMoneyModal, QuickFillModal, TargetsModal, type PickGroup, type PickItem } from './SimpleModals';
import { BudgetAccountsModal, TargetModal, type TargetSubject } from './BudgetDetailed';

/*
 * Budget, Simple view: selected-group workspace with current/future planning and read-only history.
 * Month money and envelope balances follow the existing server rules. Negative ready-to-assign
 * money stays visible as an over-planning issue. Budget options preserves the advanced tools
 * and Detailed view. Successful changes use the shared twelve-second Undo message.
 */

interface Msg {
  key: number;
  title: string;
  body?: string;
  undo?: () => void;
}

type Picker = { mode: 'cover'; target: string } | { mode: 'income'; s0: Snapshot | null } | { mode: 'plans'; s0: Snapshot | null };
type ModalKind = 'move' | 'targets' | 'quick' | 'accounts' | 'income' | null;

const HELP_PREF = 'budget.helpHidden';
const CARRY_PREF = 'budget.showCarry';
const GROUP_PREF = 'budget.selectedGroup';
const isBool = (v: unknown): v is boolean => typeof v === 'boolean';
const money = (v: number) => formatMoney(v);
const dollars = (v: number) => (Math.abs(v - Math.round(v)) < 0.005 ? formatMoney(v, { cents: false }) : formatMoney(v));
const lineOf = (bm: BudgetMonth, id: string) => bm.categories.find((c) => c.category === id);

/** A target as PATCH /api/categories takes it (to put an earlier one back on Undo). */
function targetPatch(t: CategoryTarget | null): CategoryPatch['target'] {
  if (!t) return null;
  if (t.kind === 'bills') return { kind: 'bills' };
  return t.kind === 'by_date' && t.date ? { kind: 'by_date', amount: t.amount, date: t.date } : { kind: 'monthly', amount: t.amount };
}

/** What the calendar's Add dialog needs, loaded when "+ Add a bill" is first used. */
interface BillDialogData {
  today: string;
  horizonEnd: string;
  checkingId: number | null;
  cards: Account[];
  categories: TxnCategory[];
}

export function BudgetSimple({ focusCat, onFocused, onDetailed }: { focusCat: string | null; onFocused: () => void; onDetailed: () => void }) {
  const toast = useToast();
  const currentMonth = monthOf(new Date());
  const [month, setMonth] = useState(currentMonth);
  const readOnly = month < currentMonth;
  const { bm, budget, good, busy, enqueue, snapshot, latestOf, applyMonth, saveBody, restore } = useBudgetMonth(month, { review: false });
  const [msg, setMsg] = useState<Msg | null>(null);
  const msgKey = useRef(0);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const [helpHidden, setHelpHidden] = useState(() => readPref(HELP_PREF, false, isBool));
  const [showCarry, setShowCarry] = useState(() => readPref(CARRY_PREF, false, isBool));
  const [picker, setPicker] = useState<Picker | null>(null);
  const [pickBusy, setPickBusy] = useState(false);
  const [modal, setModal] = useState<ModalKind>(null);
  const [targetFor, setTargetFor] = useState<TargetSubject | null>(null);
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const [addingGroup, setAddingGroup] = useState(false);
  const [flashId, setFlashId] = useState<string | null>(null);
  // Selected category panel, "+ Add a bill" and Move money from a category.
  const [openId, setOpenId] = useState<string | null>(null);
  const previousOpenId = useRef<string | null>(null);
  const [billFor, setBillFor] = useState<EnvelopeLine | null>(null);
  const [billData, setBillData] = useState<BillDialogData | null>(null);
  const [moveFrom, setMoveFrom] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState(() => readPref(GROUP_PREF, '', (v): v is string => typeof v === 'string'));
  const [allocationOpen, setAllocationOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [transactionsOpen, setTransactionsOpen] = useState(false);
  const editorGuard = useRef<(() => Promise<boolean>) | null>(null);
  const registerEditor = useCallback((guard: (() => Promise<boolean>) | null) => { editorGuard.current = guard; }, []);
  const transitionBusy = useRef(false);
  const todayISO = toISODate(new Date());

  const name = monthName(month);
  const savingsId = bm?.savings_category ?? null;
  const savingsName = bm && savingsId ? (lineOf(bm, savingsId)?.name ?? bm.addable.find((a) => a.id === savingsId)?.name ?? null) : null;

  function say(title: string, body?: string, undo?: () => void) {
    msgKey.current += 1;
    setMsg({ key: msgKey.current, title, body, undo });
  }
  const undoTo = (snap: Snapshot) => () => void restore(snap);

  // The category panel owns its draft. Leave it in place if validation or a save fails.
  async function afterEdit(run: () => void) {
    if (transitionBusy.current) return;
    transitionBusy.current = true;
    try {
      if (editorGuard.current && !(await editorGuard.current())) return;
      run();
    } finally {
      transitionBusy.current = false;
    }
  }

  function selectGroup(key: string) {
    setSelectedKey(key);
    writePref(GROUP_PREF, key);
  }

  function changeMonth(next: string) {
    void afterEdit(() => {
      setPicker(null);
      setModal(null);
      setTargetFor(null);
      setAddingTo(null);
      setAddingGroup(false);
      setAllocationOpen(false);
      setOpenId(null);
      setMsg(null);
      setMonth(next);
    });
  }

  // Only return focus after a guarded close has actually succeeded.
  useEffect(() => {
    const previous = previousOpenId.current;
    previousOpenId.current = openId;
    if (previous && !openId) {
      (document.getElementById(`bud-row-${previous}`)?.querySelector('.bud-category-card-toggle') as HTMLButtonElement | null)?.focus({ preventScroll: true });
    }
  }, [openId]);

  // Groups in position order (each with its hue), then categories without a group.
  const sections = useMemo<Section[]>(() => {
    if (!bm) return [];
    const groups = [...bm.groups].sort((a, b) => a.position - b.position || a.id - b.id);
    const known = new Set(groups.map((g) => g.id));
    const out: Section[] = groups.map((g, i) => ({
      key: String(g.id),
      groupId: g.id,
      name: g.name,
      hue: groupHueAt(i),
      lines: bm.categories.filter((c) => c.group_id === g.id),
    }));
    const loose = bm.categories.filter((c) => c.group_id === null || !known.has(c.group_id));
    if (loose.length || !groups.length) out.push({ key: 'none', groupId: null, name: groups.length ? 'Not in a group' : 'My categories', hue: UNGROUPED_HUE, lines: loose });
    return out;
  }, [bm]);

  const selectedSection = sections.find((s) => s.key === selectedKey) ?? sections[0];
  useEffect(() => {
    if (selectedSection && selectedSection.key !== selectedKey) selectGroup(selectedSection.key);
  }, [selectedSection, selectedKey]);

  // `?cat=`: select the category's group, reveal its panel and focus its card.
  useEffect(() => {
    if (!focusCat || !bm) return;
    const section = sections.find((s) => s.lines.some((c) => c.category === focusCat));
    if (section && selectedKey !== section.key) {
      void afterEdit(() => selectGroup(section.key));
      return;
    }
    onFocused();
    const row = document.getElementById(`bud-row-${focusCat}`);
    if (!row) {
      // E.g. a bill's category from Bills and paychecks that isn't in this month's plan.
      const spent = bm.unbudgeted.find((u) => u.category === focusCat);
      const name = spent?.name ?? bm.addable.find((a) => a.id === focusCat)?.name;
      if (name)
        toast.push({
          tone: 'info',
          title: `${name} isn’t in your plan yet`,
          body: spent ? 'It’s under “Spending without a plan”, where you can add it.' : 'You can add it from the Detailed view (More options).',
        });
      return;
    }
    row.scrollIntoView({ block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    // Its plan and transactions open too, like a click on the card.
    setOpenId(focusCat);
    (row.querySelector('.bud-category-card-toggle') as HTMLButtonElement | null)?.focus({ preventScroll: true });
    setFlashId(focusCat);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusCat, bm, selectedKey]);

  useEffect(() => {
    if (!flashId) return;
    const timer = window.setTimeout(() => setFlashId(null), 2400);
    return () => window.clearTimeout(timer);
  }, [flashId]);

  // A Cover it picker closes by itself once the category isn't over any more.
  useEffect(() => {
    if (picker?.mode !== 'cover' || !bm) return;
    const t = lineOf(bm, picker.target);
    if (!t || t.available > -0.005) setPicker(null);
  }, [picker, bm]);

  if (budget.error && !bm) return <ErrorPanel error={budget.error} onRetry={budget.reload} />;

  const earliest = good.current?.earliest_month ?? null;
  const latest = good.current?.latest_month ?? addMonths(currentMonth, 12);

  const header = (
    <header className="bud-head">
      <div>
        <h1 ref={titleRef} tabIndex={-1}>
          Budget
        </h1>
        <p className="bud-head-sub">
          {monthLong(month)}
          {bm ? ` · ${readOnly ? 'Past month · changes are read-only' : bm.is_current ? bm.days_left === 0 ? 'last day of the month' : `${plural(bm.days_left, 'day')} left` : 'Planning ahead'}` : ''}
        </p>
      </div>
      <div className="bud-head-actions">
        <button type="button" className="bud-btn" onClick={() => changeMonth(addMonths(month, -1))} disabled={busy > 0 || (earliest !== null && month <= earliest)} aria-label="Previous month"><Icon name="chevronLeft" /></button>
        <button type="button" className="bud-btn" onClick={() => changeMonth(addMonths(month, 1))} disabled={busy > 0 || month >= latest} aria-label="Next month"><Icon name="chevronRight" /></button>
        {month !== currentMonth && <button type="button" className="bud-btn" onClick={() => changeMonth(currentMonth)} disabled={busy > 0}>This month</button>}
        {bm && !bm.setup_needed && <button type="button" className="bud-btn" aria-expanded={optionsOpen} aria-controls="budget-options" onClick={() => void afterEdit(() => setOptionsOpen(!optionsOpen))}>Budget options</button>}
      </div>
    </header>
  );

  if (!bm) {
    return (
      <div className="bud" aria-busy="true">
        {header}
        <SkeletonRows rows={8} label="Loading your budget" />
      </div>
    );
  }

  // ---- actions (each shows a message with Undo)

  async function runSetup(body: BudgetSetupInput): Promise<string | null> {
    if (month !== currentMonth) return 'Set up your budget in the current month.';
    try {
      const next = await enqueue(() => api.budgets.setup(body));
      applyMonth(next);
      setHelpHidden(false);
      writePref(HELP_PREF, false);
      const kept = next.savings_category ? (lineOf(next, next.savings_category)?.carryover ?? 0) : 0;
      say('Your plan is ready.', kept > 0.004 ? `Change any amount by clicking it. ${dollars(kept)} is in Emergency savings.` : 'Change any amount by clicking it.');
      window.scrollTo({ top: 0 });
      return null;
    } catch (err) {
      return errorMessage(err);
    }
  }

  async function changePlan(c: EnvelopeLine, value: number): Promise<string | null> {
    if (readOnly) return 'Past months are read-only. Make changes in the current month.';
    const out = { blocked: '' };
    const res = await saveBody((cur) => {
      const line = lineOf(cur, c.category);
      if (!line) { out.blocked = 'This category is no longer in your plan. Reload the budget and try again.'; return null; }
      const extra = round2(value - line.assigned);
      if (Math.abs(extra) < 0.005) return null;
      if (extra > 0.004 && extra > Math.max(0, cur.ready_to_assign) + 0.004) {
        const over = round2(extra - cur.ready_to_assign);
        out.blocked = `That would plan ${money(over)} more than ${monthName(cur.month)}’s ${money(cur.month_money)}. Lower another plan first, or change the month’s amount.`;
        return null;
      }
      return { assigned: { [c.category]: value } };
    }, `Couldn’t change ${c.name}`, (message) => { out.blocked = message; });
    if (out.blocked) return out.blocked;
    if (res) say(`${c.name} plan changed to ${money(value)}.`, undefined, undoTo(res.snap));
    return null;
  }

  async function allocate(category: string, amount: number): Promise<string | null> {
    if (readOnly) return 'Past months are read-only. Make changes in the current month.';
    const out = { error: '' };
    const res = await saveBody((cur) => {
      const line = lineOf(cur, category);
      if (!line) { out.error = 'This category is no longer in your plan. Choose another category.'; return null; }
      if (!Number.isFinite(amount) || amount < 0.005) { out.error = 'Type an amount greater than zero.'; return null; }
      const addition = round2(amount);
      if (addition > Math.max(0, cur.ready_to_assign) + 0.004) {
        out.error = `Only ${money(Math.max(0, cur.ready_to_assign))} is not planned yet. Use a smaller amount or move money from another category.`;
        return null;
      }
      return { assigned: { [category]: round2(line.assigned + addition) } };
    }, 'Couldn’t give the money a plan', (message) => { out.error = message; });
    if (out.error) { budget.reload(); return out.error; }
    if (res) say(`Added ${money(amount)} to ${lineOf(res.next, category)?.name ?? 'the category'}’s plan.`, undefined, undoTo(res.snap));
    return null;
  }

  async function saveAmount(mode: BudgetIncomeMode, amount: number | null): Promise<string | null> {
    if (month !== currentMonth) return 'The month’s amount can only be changed for this month.';
    const cur = bm!;
    if (mode !== cur.income.mode) {
      const was = cur.income.mode;
      try {
        applyMonth(await enqueue(() => api.budgets.settings({ income_mode: mode })));
      } catch (err) {
        return errorMessage(err);
      }
      say(
        mode === 'expected' ? 'Now counting the paychecks you expect this month.' : 'Now counting only money that’s already in your accounts.',
        undefined,
        () =>
          void enqueue(async () => {
            try {
              applyMonth(await api.budgets.settings({ income_mode: was }));
            } catch (err) {
              toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
            }
          }),
      );
      if (mode === 'off') return null;
    }
    if (mode !== 'expected' || amount === null) return null;
    const out = { err: '' };
    const res = await saveBody(
      (c) => {
        if (Math.abs(amount - c.month_money) < 0.005) return null;
        // The month's amount moves one-for-one with the expected income.
        const base = Math.max(c.income.expected ?? c.income.received, c.income.received);
        return { income_expected: round2(base + (amount - c.month_money)) };
      },
      'Couldn’t change the amount',
      (m) => (out.err = m),
    );
    if (out.err) return out.err;
    if (res) say(`${name}’s amount is now ${money(amount)}.`, undefined, undoTo(res.snap));
    return null;
  }

  async function onMore(choice: 'add' | 'save' | 'leave') {
    if (month !== currentMonth) return;
    const ev = bm!.income.event;
    if (!ev || ev.kind !== 'more') return;
    const res = await saveBody((cur) => {
      const e = cur.income.event;
      if (!e || e.kind !== 'more') return null;
      const body: BudgetSave = { income_expected: e.received };
      // One-time: this month's extra pay goes to savings; next month's plan stays as it was.
      if (choice === 'save' && savingsId) {
        // Savings not in this month's plan yet starts at $0 (then gets the one-time amount).
        const moved = oneTimeMove([...cur.categories, { category: savingsId, assigned: 0, moved: 0 }], { [savingsId]: e.amount });
        if (moved) Object.assign(body, moved);
      }
      return body;
    }, 'Couldn’t change the plan');
    if (!res) return;
    const amt = dollars(ev.amount);
    if (choice === 'add') say(`${amt} added. It’s under Not planned yet.`, undefined, undoTo(res.snap));
    else if (choice === 'save') say(`${amt} added to ${savingsName}.`, undefined, undoTo(res.snap));
    else say(`Okay. The ${amt} stays under Not planned yet.`, 'Money that isn’t planned carries into next month.', undoTo(res.snap));
  }

  async function onLess(choice: 'unplanned' | 'categories' | 'savings' | 'next') {
    if (month !== currentMonth) return;
    const ev = bm!.income.event;
    if (!ev || ev.kind !== 'less') return;
    if (choice === 'categories') return setPicker({ mode: 'income', s0: null });
    const took = { amount: 0 };
    const res = await saveBody((cur) => {
      const e = cur.income.event;
      if (!e || e.kind !== 'less') return null;
      if (choice === 'unplanned') return { income_expected: e.received };
      if (choice === 'next') return { income_expected: e.received, accept_received: true };
      const s = savingsId ? lineOf(cur, savingsId) : undefined;
      const y = round2(Math.min(e.amount, Math.max(0, s?.available ?? 0)));
      if (!s || y < 0.005) return null;
      took.amount = y;
      // One-time: covers this month's short paycheck only.
      return { income_expected: round2(e.expected - y), ...oneTimeMove(cur.categories, { [s.category]: -y }) };
    }, 'Couldn’t change the plan');
    if (!res) return;
    const short = dollars(ev.amount);
    if (choice === 'unplanned') say(`Done. ${short} came out of Not planned yet. No plans changed.`, undefined, undoTo(res.snap));
    else if (choice === 'next') say(`${monthName(addMonths(month, 1))}’s money will start ${short} lower.`, undefined, undoTo(res.snap));
    else {
      const left = round2(ev.amount - took.amount);
      say(`Took ${money(took.amount)} from ${savingsName}.`, left > 0.004 ? `${money(left)} is still short.` : undefined, undoTo(res.snap));
    }
  }

  async function take(item: PickItem, amount: number) {
    if (readOnly || !picker || !Number.isFinite(amount) || amount < 0.005) return;
    setPickBusy(true);
    try {
      if (picker.mode === 'cover') {
        const target = picker.target;
        const out = { error: '' };
        const res = await saveBody((cur) => {
          const t = lineOf(cur, target);
          if (!t) return null;
          const source = item.id ? lineOf(cur, item.id) : null;
          const available = item.id ? Math.max(0, source?.available ?? 0) : Math.max(0, cur.ready_to_assign);
          if (amount > available + 0.004 || amount > Math.max(0, -t.available) + 0.004) {
            out.error = 'The available money has changed. Choose an amount using the updated balances.';
            return null;
          }
          // One-time: the plans that repeat next month stay as they were.
          return oneTimeMove(cur.categories, item.id ? { [target]: amount, [item.id]: -amount } : { [target]: amount });
        }, 'Couldn’t move the money', (message) => { out.error = message; });
        if (out.error) { budget.reload(); toast.push({ tone: 'error', title: 'Couldn’t cover the category', body: out.error }); return; }
        if (!res) return;
        if (res) {
          const t = lineOf(res.next, target);
          if (!t || t.available > -0.005) setPicker(null);
          const still = t && t.available < -0.004 ? ` Still over by ${money(-t.available)}.` : '';
          say(`Moved ${money(amount)} from ${item.name} to ${t?.name ?? 'the category'}.${still}`, undefined, undoTo(res.snap));
        }
        return;
      }
      if (picker.mode === 'income') {
        const res = await saveBody((cur) => {
          const e = cur.income.event;
          if (!e || e.kind !== 'less') return null;
          const body: BudgetSave = { income_expected: round2(e.expected - amount) };
          if (item.id) {
            // One-time: lowered for this month's short paycheck only.
            const moved = oneTimeMove(cur.categories, { [item.id]: -amount });
            if (!moved) return null;
            Object.assign(body, moved);
          }
          return body;
        }, 'Couldn’t lower the plan');
        if (!res) return;
        const first = picker.s0 ?? res.snap;
        const ev = res.next.income.event;
        if (ev && ev.kind === 'less') return setPicker({ mode: 'income', s0: first });
        setPicker(null);
        say(`Done. ${name}’s plan now matches the ${dollars(res.next.income.received)} that came in.`, undefined, undoTo(first));
        return;
      }
      if (!item.id) return;
      const id = item.id;
      const res = await saveBody((cur) => {
        const s = lineOf(cur, id);
        return s ? { assigned: { [id]: round2(s.assigned - amount) } } : null;
      }, 'Couldn’t lower the plan');
      if (!res) return;
      const first = picker.s0 ?? res.snap;
      if (res.next.ready_to_assign < -0.004) return setPicker({ mode: 'plans', s0: first });
      setPicker(null);
      say(`Done. Your plans now fit ${name}’s money.`, undefined, undoTo(first));
    } finally {
      setPickBusy(false);
    }
  }

  async function addToPlan(u: SpendLine) {
    if (readOnly) return;
    const res = await saveBody(() => ({ assigned: { [u.category]: u.spent } }), `Couldn’t add ${u.name}`);
    if (!res) return;
    say(`${u.name} is in your plan now, with ${money(u.spent)}.`, undefined, () =>
      void enqueue(async () => {
        try {
          applyMonth(await api.budgets.save(month, { removed: [u.category] }));
        } catch (err) {
          toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
        }
      }),
    );
  }

  // ---- phase 2: bills and the row's details

  /** A plan change with the Simple view's "more than the month's money" block. */
  async function planTo(c: EnvelopeLine, value: number, title: string): Promise<{ snap: Snapshot } | null> {
    if (readOnly) return null;
    const out = { blocked: '' };
    const res = await saveBody((cur) => {
      const line = lineOf(cur, c.category);
      if (!line) return null;
      const extra = round2(value - line.assigned);
      if (Math.abs(extra) < 0.005) return null;
      if (extra > 0.004 && extra > Math.max(0, cur.ready_to_assign) + 0.004) {
        const over = round2(extra - cur.ready_to_assign);
        out.blocked = `That would plan ${money(over)} more than ${monthName(cur.month)}’s ${money(cur.month_money)}. Lower another plan first, or change the month’s amount.`;
        return null;
      }
      return { assigned: { [c.category]: value } };
    }, title);
    if (out.blocked) say(out.blocked);
    return res;
  }

  async function planBills(c: EnvelopeLine) {
    const total = c.bills?.total ?? 0;
    const res = await planTo(c, total, `Couldn’t change ${c.name}`);
    if (res) say(`Planned ${moneyShort(total)} for ${c.name}’s bills.`, undefined, undoTo(res.snap));
  }

  /** "Always plan for my bills": the bills target, then this month's plan; one Undo puts both back. */
  async function alwaysBills(c: EnvelopeLine) {
    if (readOnly) return;
    const before = c.target ? targetOf(c.target) : null;
    try {
      await enqueue(() => api.categories.update(c.category, { target: { kind: 'bills' } }));
    } catch (err) {
      toast.push({ tone: 'error', title: `Couldn’t change ${c.name}`, body: errorMessage(err) });
      return;
    }
    const total = c.bills?.total ?? 0;
    const res = await planTo(c, total, `Couldn’t change ${c.name}`);
    if (!res) applyMonth(await enqueue(() => api.budgets.get(month)));
    say(`${c.name} will plan for its bills every month.`, res ? `${moneyShort(total)} is planned for ${name}.` : undefined, () => {
      void enqueue(async () => {
        try {
          // restore: a by-date target whose date has passed since goes back as it was.
          await api.categories.update(c.category, before ? { target: targetPatch(before), restore: true } : { target: null });
          if (!res) applyMonth(await api.budgets.get(month));
        } catch (err) {
          toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
        }
      });
      if (res) void restore(res.snap);
    });
  }

  async function openAddBill(c: EnvelopeLine) {
    if (readOnly) return;
    if (!billData) {
      try {
        const [cal, accounts, categories] = await Promise.all([
          api.forecast.calendar({ from: todayISO, to: todayISO }),
          api.accounts.list(),
          api.categories.list(),
        ]);
        setBillData({
          today: cal.today,
          horizonEnd: cal.horizon_end,
          checkingId: cal.account?.id ?? null,
          cards: accounts.filter((a) => !a.hidden && a.category === 'credit'),
          categories,
        });
      } catch (err) {
        toast.push({ tone: 'error', title: 'Couldn’t open Add a bill', body: errorMessage(err) });
        return;
      }
    }
    setBillFor(c);
  }

  async function addBill(input: RecurringInput): Promise<string | null> {
    if (readOnly) return 'Past months are read-only. Make changes in the current month.';
    const c = billFor;
    try {
      const item = await api.recurring.create(input);
      setBillFor(null);
      applyMonth(await enqueue(() => api.budgets.get(month)));
      say(`${item.name} added${c ? ` to ${c.name}` : ''}.`, 'It’s on your calendar too.', () =>
        void enqueue(async () => {
          try {
            await api.recurring.remove(item.id);
            applyMonth(await api.budgets.get(month));
          } catch (err) {
            toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
          }
        }),
      );
      return null;
    } catch (err) {
      return errorMessage(err);
    }
  }

  async function renameCategory(c: EnvelopeLine, next: string): Promise<string | null> {
    if (readOnly) return 'Past months are read-only. Make changes in the current month.';
    const was = c.name;
    try {
      await enqueue(() => api.categories.update(c.category, { name: next }));
      applyMonth(await enqueue(() => api.budgets.get(month)));
      say(`${was} is now ${next}.`, undefined, () =>
        void enqueue(async () => {
          try {
            await api.categories.update(c.category, { name: was });
            applyMonth(await api.budgets.get(month));
          } catch (err) {
            toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
          }
        }),
      );
      return null;
    } catch (err) {
      return errorMessage(err);
    }
  }

  async function removeCategory(c: EnvelopeLine) {
    if (readOnly) return;
    const plan = c.assigned;
    const moved = c.moved;
    try {
      applyMonth(await enqueue(() => api.budgets.save(month, { removed: [c.category] })));
    } catch (err) {
      toast.push({ tone: 'error', title: `Couldn’t remove ${c.name}`, body: errorMessage(err) });
      return;
    }
    setOpenId(null);
    titleRef.current?.focus({ preventScroll: true });
    say(`${c.name} is out of your plan.`, 'Its spending shows under Spending without a plan.', () =>
      void enqueue(async () => {
        try {
          // restore: takes the removal back, so the envelope (and a negative plan) is exactly as it was.
          applyMonth(await api.budgets.save(month, { assigned: { [c.category]: plan }, ...(Math.abs(moved) >= 0.005 ? { moved: { [c.category]: moved } } : {}), restore: true }));
        } catch (err) {
          toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
        }
      }),
    );
  }

  /** The page again after a delete or its Undo; a failed read falls back to a full reload. */
  async function reloadMonth() {
    try {
      applyMonth(await api.budgets.get(month));
    } catch {
      budget.reload();
    }
  }

  // Delete everywhere (the user's own categories only), like Settings › Categories, with the same Undo.
  async function deleteCategory(c: EnvelopeLine) {
    if (readOnly) return;
    let snap: CategorySnapshot;
    try {
      snap = await enqueue(async () => {
        const taken = await api.categories.snapshot(c.category);
        await api.categories.remove(c.category);
        return taken;
      });
    } catch (err) {
      toast.push({ tone: 'error', title: `Couldn’t delete ${c.name}`, body: errorMessage(err) });
      return;
    }
    // Queued like every other read, so a later save can't be overwritten by this older month.
    await enqueue(reloadMonth);
    setOpenId(null);
    titleRef.current?.focus({ preventScroll: true });
    const n = snap.rules.length;
    const rulesNote = n === 0 ? '' : n === 1 ? ' The rule that used it was deleted too.' : ` The ${n} rules that used it were deleted too.`;
    say(`${c.name} was deleted.`, `Its purchases go back to automatic sorting.${rulesNote}`, () =>
      void enqueue(async () => {
        try {
          await api.categories.restore(snap);
        } catch (err) {
          toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
        }
        await reloadMonth();
      }),
    );
  }

  // Its categories stay, without a group (Settings does the same); Undo puts them back in it.
  async function deleteGroup(s: Section) {
    const id = s.groupId;
    if (readOnly || id === null) return;
    let snap: CategoryGroupSnapshot;
    try {
      snap = await enqueue(async () => {
        const groups = [...(await api.categoryGroups.list())].sort((a, b) => a.position - b.position || a.id - b.id);
        const g = groups.find((x) => x.id === id);
        const taken = { id, name: g?.name ?? s.name, position: Math.max(0, groups.findIndex((x) => x.id === id)), category_ids: g?.category_ids ?? s.lines.map((c) => c.category) };
        await api.categoryGroups.remove(id);
        return taken;
      });
    } catch (err) {
      toast.push({ tone: 'error', title: 'Couldn’t delete the group', body: errorMessage(err) });
      return;
    }
    await enqueue(reloadMonth);
    setOpenId(null);
    setAddingTo(null);
    if (s.lines.length) selectGroup('none');
    titleRef.current?.focus({ preventScroll: true });
    say(`${s.name} group was deleted.`, s.lines.length ? 'Its categories stay in your budget, without a group.' : undefined, () =>
      void enqueue(async () => {
        let back: string | null = null;
        try {
          // The server keeps the old id only if it's still free: find the group by name.
          const groups = await api.categoryGroups.restore(snap);
          const g = groups.find((x) => x.id === id) ?? groups.find((x) => x.name.toLowerCase() === snap.name.toLowerCase());
          back = g ? String(g.id) : null;
        } catch (err) {
          toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
        }
        await reloadMonth();
        // After the reload: selecting a group the page doesn't have yet falls back to the first one.
        if (back) selectGroup(back);
      }),
    );
  }

  const rowActions: RowActions = {
    onPlan: changePlan,
    onCover: (c) => void afterEdit(() => { if (!readOnly) setPicker({ mode: 'cover', target: c.category }); }),
    onPlanBills: (c) => void afterEdit(() => { void planBills(c); }),
    onAlwaysBills: (c) => void afterEdit(() => { void alwaysBills(c); }),
    onAddBill: (c) => void afterEdit(() => { void openAddBill(c); }),
    onTarget: (c) => void afterEdit(() => { if (!readOnly) setTargetFor({ id: c.category, name: c.name, target: c.target ? targetOf(c.target) : null, carryover: c.carryover, bills: c.bills?.total ?? null }); }),
    onMove: (c) => void afterEdit(() => {
      if (readOnly) return;
      setMoveFrom(c.category);
      setModal('move');
    }),
    onRename: renameCategory,
    onRemove: (c) => void afterEdit(() => { void removeCategory(c); }),
    onDelete: (c) => void afterEdit(() => { void deleteCategory(c); }),
  };

  async function addCategory(section: Section, catName: string, plan: number): Promise<string | null> {
    if (readOnly) return 'Past months are read-only. Make changes in the current month.';
    try {
      const res = await enqueue(() => api.budgets.addCategory(month, { name: catName, group_id: section.groupId, plan }));
      applyMonth(res.month);
      setAddingTo(null);
      setFlashId(res.category.id);
      // The form goes away: keep the keyboard on the new category card.
      window.requestAnimationFrame(() =>
        (document.querySelector(`#bud-row-${CSS.escape(res.category.id)} .bud-category-card-toggle`) as HTMLButtonElement | null)?.focus({ preventScroll: true }),
      );
      say(`${res.category.name} added to ${section.name}.`, undefined, () =>
        void enqueue(async () => {
          try {
            await api.categories.remove(res.category.id);
            applyMonth(await api.budgets.get(month));
          } catch (err) {
            toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
          }
        }),
      );
      return null;
    } catch (err) {
      return errorMessage(err);
    }
  }

  async function addGroup(groupName: string): Promise<string | null> {
    if (readOnly) return 'Past months are read-only. Make changes in the current month.';
    try {
      const g = await enqueue(() => api.categoryGroups.create(groupName));
      applyMonth(await api.budgets.get(month));
      setAddingGroup(false);
      selectGroup(String(g.id));
      setAddingTo(String(g.id));
      say(`${g.name} group added.`, 'Now add its first category.', () =>
        void enqueue(async () => {
          try {
            await api.categoryGroups.remove(g.id);
            applyMonth(await api.budgets.get(month));
          } catch (err) {
            toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
          }
        }),
      );
      return null;
    } catch (err) {
      return errorMessage(err);
    }
  }

  async function moveMoney(from: EnvelopeLine, to: string, amount: number): Promise<string | null> {
    if (readOnly) return 'Past months are read-only. Make changes in the current month.';
    const out = { err: '' };
    const res = await saveBody(
      (cur) => {
        const f = lineOf(cur, from.category);
        if (!f) return null;
        const movable = Math.max(0, round2(f.carryover + f.assigned));
        if (!Number.isFinite(amount) || amount < 0.005 || amount > movable + 0.004) {
          out.err = `You can move at most ${money(movable)} out of ${f.name}. Use a smaller amount.`;
          return null;
        }
        // One-time: the plans that repeat next month stay as they were.
        return oneTimeMove(cur.categories, to === 'rta' ? { [f.category]: -amount } : { [f.category]: -amount, [to]: amount });
      },
      'Couldn’t move the money',
      (m) => (out.err = m),
    );
    if (out.err) return out.err;
    if (res) {
      setModal(null);
      const toName = to === 'rta' ? 'Not planned yet' : (lineOf(res.next, to)?.name ?? 'the category');
      say(`Moved ${money(amount)} from ${from.name} to ${toName}.`, undefined, undoTo(res.snap));
    }
    return null;
  }

  function fundTargets() {
    if (readOnly) return;
    void enqueue(async () => {
      try {
        const snap = snapshot(await latestOf(month));
        const res = await api.fundTargets(month);
        applyMonth(res.month);
        setModal(null);
        if (res.funded < 0.005) say('Nothing to fill targets with.', `Targets still need ${money(res.unfunded)}, and nothing is left to plan.`);
        else say(`Gave targets ${money(res.funded)}.`, res.unfunded > 0.004 ? `${money(res.unfunded)} is still needed.` : undefined, undoTo(snap));
      } catch (err) {
        toast.push({ tone: 'error', title: 'Couldn’t fill targets', body: errorMessage(err) });
      }
    });
  }

  async function quickFill(assigned: Record<string, number>, title: string) {
    if (readOnly) return;
    setModal(null);
    const res = await saveBody((cur) => {
      const changed = Object.fromEntries(Object.entries(assigned).filter(([id, v]) => Math.abs(v - (lineOf(cur, id)?.assigned ?? Number.NaN)) >= 0.005));
      return Object.keys(changed).length ? { assigned: changed } : null;
    }, 'Couldn’t fill in the plans');
    if (res) say(`${title}: done.`, undefined, undoTo(res.snap));
  }

  async function saveIncomeSettings(mode: BudgetIncomeMode, savings: string | null): Promise<string | null> {
    if (month !== currentMonth) return 'Change expected income and savings in the current month.';
    const was = { income_mode: bm!.income.mode, savings_category: bm!.savings_category };
    try {
      applyMonth(await enqueue(() => api.budgets.settings({ income_mode: mode, savings_category: savings })));
      setModal(null);
      const changed = mode !== was.income_mode || savings !== was.savings_category;
      say(
        mode !== was.income_mode
          ? mode === 'expected'
            ? 'Now counting the paychecks you expect this month.'
            : 'Now counting only money that’s already in your accounts.'
          : 'Saved.',
        undefined,
        changed
          ? () =>
              void enqueue(async () => {
                try {
                  applyMonth(await api.budgets.settings(was));
                } catch (err) {
                  toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
                }
              })
          : undefined,
      );
      return null;
    } catch (err) {
      return errorMessage(err);
    }
  }

  // ---- the source picker's lists

  let pick: { title: string; sub: string; need: number; groups: PickGroup[]; noneText: string } | null = null;
  if (picker) {
    const target = picker.mode === 'cover' ? lineOf(bm, picker.target) : undefined;
    const ev = bm.income.event;
    const need =
      picker.mode === 'cover'
        ? round2(Math.max(0, -(target?.available ?? 0)))
        : picker.mode === 'income'
          ? ev && ev.kind === 'less'
            ? ev.amount
            : 0
          : round2(Math.max(0, -bm.ready_to_assign));
    const groups: PickGroup[] = [];
    if (picker.mode !== 'plans' && bm.ready_to_assign > 0.004) {
      const avail = round2(bm.ready_to_assign);
      groups.push({
        key: 'rta',
        name: 'Money without a plan',
        hue: null,
        items: [{ id: null, name: 'Not planned yet', avail, availText: `${money(avail)} available${picker.mode === 'income' ? ' · no plans change' : ''}` }],
      });
    }
    for (const s of sections) {
      const items = s.lines
        .filter((c) => c.category !== target?.category && c.available > 0.004)
        .map((c) => ({
          id: c.category,
          name: c.name,
          avail: c.available,
          availText: `${money(c.available)} left${c.goal ? ' · Reserved for a goal' : c.debt ? ' · Reserved for debt payments' : c.category === savingsId ? ' · Reserved for savings' : ''}`,
        }));
      if (items.length) groups.push({ key: s.key, name: s.name, hue: s.hue, items });
    }
    pick = {
      title: picker.mode === 'cover' ? `Cover ${money(need)} for ${target?.name ?? 'this category'}` : `Lower plans by ${money(need)}`,
      sub: picker.mode === 'cover' ? 'Pick where to take it from.' : 'Pick where to take it from. You can pick more than one.',
      need,
      groups,
      noneText:
        picker.mode === 'cover'
          ? `No money is free right now, so the extra ${money(need)} will come out of next month’s money.`
          : picker.mode === 'income'
            ? 'No category has money left. Choose “Leave it for next month” instead.'
            : 'No category has money left to lower.',
    };
  }

  const savingsLine = savingsId ? lineOf(bm, savingsId) : undefined;
  const notPlanned = Math.max(0, bm.ready_to_assign);
  const tiles: MoreTile[] = [
    { key: 'move', title: 'Move money between categories', body: 'Take from one plan, add to another.', run: () => setModal('move') },
    { key: 'targets', title: 'Targets', body: 'Save toward a set amount each month, or by a date.', run: () => setModal('targets') },
    {
      key: 'carry',
      title: 'Show carried-over amounts',
      body: 'Add a column for money left from last month.',
      pressed: showCarry,
      run: () => {
        setShowCarry(!showCarry);
        writePref(CARRY_PREF, !showCarry);
      },
    },
    { key: 'quick', title: 'Quick fill', body: 'Use last month’s plans, or last month’s spending.', run: () => setModal('quick') },
    { key: 'accounts', title: 'Budget accounts', body: 'Choose which accounts hold your budget money.', run: () => setModal('accounts') },
    { key: 'income', title: 'Expected income and savings', body: 'Count expected paychecks or only money already in, and pick your savings category.', run: () => setModal('income') },
    { key: 'detailed', title: 'Detailed view', body: 'The full budget table with every tool.', run: onDetailed },
    { key: 'help', title: 'How this page works', body: 'A guide to planning your money.', run: () => { setHelpHidden(false); writePref(HELP_PREF, false); setOptionsOpen(false); } },
  ];
  const shownTiles = tiles.filter((t) => (!readOnly || ['carry', 'detailed', 'help'].includes(t.key)) && (month === currentMonth || !['income', 'accounts'].includes(t.key)));

  return (
    <div className="bud">
      {header}

      {optionsOpen && !bm.setup_needed && (
        <section id="budget-options" className="bud-more" aria-label="Budget options">
          <div className="bud-more-tiles">
            {shownTiles.map((tile) => (
              <button key={tile.key} type="button" className="bud-more-tile" onClick={() => void afterEdit(tile.run)} aria-pressed={tile.pressed}>
                <span className="bud-more-tile-title">{tile.title}</span>
                <span className="bud-more-tile-body">{tile.body}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {bm.setup_needed ? (
        bm.is_current ? <SetupCard onSubmit={runSetup} /> : <section className="bud-card"><p>Set up your budget in the current month first.</p><button type="button" className="bud-btn bud-btn-primary" onClick={() => changeMonth(currentMonth)}>Go to this month</button></section>
      ) : (
        <>
          {!helpHidden && (
            <HelpCard
              onHide={() => {
                setHelpHidden(true);
                writePref(HELP_PREF, true);
              }}
            />
          )}
          {budget.error && <ErrorPanel error={budget.error} onRetry={budget.reload} />}
          <BudgetOverview bm={bm} onSaveAmount={saveAmount} />
          <IncomeNote
            bm={bm}
            savingsName={savingsName}
            savingsFree={Math.max(0, savingsLine?.available ?? 0)}
            onMore={(c) => void afterEdit(() => { void onMore(c); })}
            onLess={(c) => void afterEdit(() => { void onLess(c); })}
          />
          <BudgetPlanningStatus bm={bm} readOnly={readOnly} onAllocate={() => void afterEdit(() => setAllocationOpen(true))} onLower={() => void afterEdit(() => setPicker({ mode: 'plans', s0: null }))} />

          <GroupSelector sections={sections} savingsId={savingsId} selectedKey={selectedSection?.key ?? ''} onSelect={(key) => void afterEdit(() => { selectGroup(key); setOpenId(null); setAddingTo(null); })} onAdd={() => void afterEdit(() => setAddingGroup(true))} disabled={readOnly} />
          {addingGroup && !readOnly && <AddGroupForm onCancel={() => setAddingGroup(false)} onAdd={addGroup} />}

          <div className="bud-workspace">
            {selectedSection && (() => { const s = selectedSection; return (
              <GroupCard
                key={s.key}
                section={s}
                savingsId={savingsId}
                showCarry={showCarry}
                monthName={name}
                prevMonthName={monthName(addMonths(month, -1))}
                today={todayISO}
                month={month}
                transactionScopeKey={bm.budget_accounts.filter((account) => account.included).map((account) => account.id).join(',')}
                readOnly={readOnly}
                onRegisterEditor={registerEditor}
                onMoveMoney={() => void afterEdit(() => { setMoveFrom(null); setModal('move'); })}
                onSeeTransactions={() => void afterEdit(() => setTransactionsOpen(true))}
                notPlanned={notPlanned}
                readyToAssign={bm.ready_to_assign}
                monthMoney={bm.month_money}
                adding={addingTo === s.key}
                flashId={flashId}
                openId={openId}
                onToggle={(id) => void afterEdit(() => setOpenId((o) => (o === id ? null : id)))}
                onStartAdd={() => void afterEdit(() => { if (!readOnly) setAddingTo(s.key); })}
                onCancelAdd={() => setAddingTo(null)}
                onAdd={(n, plan) => addCategory(s, n, plan)}
                onDeleteGroup={s.groupId === null ? undefined : () => void afterEdit(() => { void deleteGroup(s); })}
                actions={rowActions}
              />
            ); })()}
          </div>

          <NotInPlanCard bm={bm} readOnly={readOnly} onAdd={(u) => void afterEdit(() => { void addToPlan(u); })} />
          {busy > 0 && (
            <p className="sr-only" aria-live="polite">
              Saving…
            </p>
          )}
        </>
      )}

      <BudgetAllocationModal open={allocationOpen && !readOnly} bm={bm} sections={sections} initialCategory={selectedSection?.lines[0]?.category ?? null} onClose={() => setAllocationOpen(false)} onAdd={allocate} onAddCategory={() => { setAllocationOpen(false); if (selectedSection) setAddingTo(selectedSection.key); }} />
      <Modal open={transactionsOpen} title="See transactions" subtitle="Choose a category in this group." onClose={() => setTransactionsOpen(false)}>
        {selectedSection?.lines.length ? <div className="bud-pick-list">{selectedSection.lines.map((c) => <button type="button" className="bud-btn" key={c.category} onClick={() => void afterEdit(() => { setTransactionsOpen(false); setOpenId(c.category); })}>{c.name}<Icon name="chevronRight" /></button>)}</div> : <p>No categories in this group yet.</p>}
      </Modal>

      {pick && (
        <CoverPicker
          open
          title={pick.title}
          sub={pick.sub}
          need={pick.need}
          groups={pick.groups}
          noneText={pick.noneText}
          busy={pickBusy}
          onTake={(item, amount) => void take(item, amount)}
          onClose={() => setPicker(null)}
        />
      )}
      <MoveMoneyModal
        open={modal === 'move'}
        sections={sections}
        savingsId={savingsId}
        initialFrom={moveFrom}
        onClose={() => {
          setModal(null);
          setMoveFrom(null);
        }}
        onMove={moveMoney}
      />
      <TargetsModal
        open={modal === 'targets' && !targetFor}
        bm={bm}
        lines={bm.categories}
        onClose={() => setModal(null)}
        onEdit={(c) => setTargetFor({ id: c.category, name: c.name, target: c.target ? targetOf(c.target) : null, carryover: c.carryover, bills: c.bills?.total ?? null })}
        onFund={fundTargets}
      />
      <TargetModal
        subject={targetFor}
        month={month}
        onClose={() => setTargetFor(null)}
        onSaved={(title, body) => {
          setTargetFor(null);
          budget.reload();
          toast.push({ tone: 'success', title, body, timeout: 3500 });
        }}
      />
      <QuickFillModal open={modal === 'quick'} bm={bm} onClose={() => setModal(null)} onFill={(a, t) => void quickFill(a, t)} onFund={fundTargets} />
      <BudgetAccountsModal
        bm={bm}
        open={modal === 'accounts'}
        onClose={() => setModal(null)}
        onSaved={(next) => {
          applyMonth(next);
          setModal(null);
          say('Budget accounts updated.');
        }}
      />
      <IncomeSettingsModal open={modal === 'income'} bm={bm} onClose={() => setModal(null)} onSave={saveIncomeSettings} />
      {billData && (
        <AddItemDialog
          open={billFor !== null}
          date={billData.today}
          today={billData.today}
          horizonEnd={billData.horizonEnd}
          checkingId={billData.checkingId}
          cards={billData.cards}
          // The budget's own lines too, so the preset category is always in the list; never
          // Emergency savings (money kept, not a bill).
          categories={[
            ...billData.categories,
            ...bm.categories
              .filter((c) => !billData.categories.some((x) => x.id === c.category))
              .map((c) => ({ id: c.category, name: c.name, hue: c.hue, kind: 'spending' as const, custom: true, hidden: false, group_id: c.group_id, target: null })),
          ].filter((c) => c.id !== bm.savings_category)}
          initialCategory={billFor?.category ?? null}
          onClose={() => setBillFor(null)}
          onAdd={addBill}
        />
      )}

      <UndoToast
        message={msg && { key: msg.key, title: msg.title, body: msg.body, undo: !!msg.undo }}
        onUndo={() => {
          if (!msg?.undo) return;
          const k = msg.key;
          msg.undo();
          setMsg((m) => (m?.key === k ? null : m));
          // The message goes away with its button: keep the keyboard on the page.
          titleRef.current?.focus({ preventScroll: true });
        }}
        onClose={() => {
          const k = msg?.key;
          setMsg((m) => (m?.key === k ? null : m));
        }}
      />
    </div>
  );
}
