import { useEffect, useId, useMemo, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent, type ReactNode } from 'react';
import { api, errorMessage, type BudgetMonth, type CategoryTarget, type EnvelopeLine, type SpendingReview } from '../../api';
import { readPref, writePref } from '../../lib/prefs';
import { useApi } from '../../lib/useApi';
import { Link } from 'react-router-dom';
import { useApp } from '../../state';
import { CATEGORY_SINGULAR, formatDate, formatMoney, plural, toISODate } from '../../lib/format';
import { addMonths, monthLong, monthName, monthOf, monthShort, round2, segmentColor, str, targetAmount, targetNeeded, targetOf } from '../../lib/budget';
import { Icon } from '../../components/Icon';
import { Money, Skeleton, SkeletonRows, parseMoneyInput } from '../../components/ui';
import { EmptyState } from '../../components/EmptyState';
import { ErrorPanel } from '../../components/ErrorPanel';
import { Modal } from '../../components/Modal';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { useToast } from '../../components/Toast';
import { useBudgetMonth } from './useBudgetMonth';
import { oneTimeMove } from './workspaceMath';

/*
 * Budget, Detailed view: the envelope budget, "Assign every dollar" (budget-assign design),
 * unchanged from Release 3.1 except that the save queue lives in `useBudgetMonth` (shared with
 * the Simple view) and Ready to assign says how much of it is income still expected.
 * Every change saves as you make it: type in an Assigned cell (Enter or blur saves, Esc
 * discards), move money between categories, or use the Assign menu and Quick assign.
 * The math and the save rules are the server's (SPEC "Release 2.1: Envelope budgets"):
 * available = carryover + assigned − spent, Ready to assign is global, a save may not lower
 * Ready to assign below $0, and a category can't go below −carryover.
 */

/** Collapsed category groups on the Spending page: group ids as strings, or 'none' for Other. */
const COLLAPSED_PREF = 'spending.collapsedGroups';
const isStringList = (v: unknown): v is string[] => Array.isArray(v) && v.every((x) => typeof x === 'string');

/** A category whose target is being edited. */
export interface TargetSubject {
  id: string;
  name: string;
  target: CategoryTarget | null;
  carryover: number;
  /** Phase 2: this month's bill total in the category (for "Cover my bills"), when known. */
  bills?: number | null;
}

type Status = 'over' | 'under' | 'pos' | 'zero';
type Filter = 'all' | 'under' | 'over' | 'avail';

/** A member category with its status (design: checked in order overspent, underfunded, available, zero). */
interface Line extends EnvelopeLine {
  /** What the target still needs this month (the server's `needed`); 0 without a target. */
  under: number;
  st: Status;
}

interface Section {
  key: string;
  name: string;
  lines: Line[];
}

function toLine(c: EnvelopeLine): Line {
  const under = c.target ? Math.max(0, c.target.needed) : 0;
  const st: Status = c.available < -0.004 ? 'over' : under > 0.004 ? 'under' : c.available > 0.004 ? 'pos' : 'zero';
  return { ...c, under, st };
}

const matches = (f: Filter, c: Line) => f === 'all' || (f === 'under' && c.st === 'under') || (f === 'over' && c.st === 'over') || (f === 'avail' && c.available > 0.004);

/** "1,516.57": the Assigned input's resting text. */
const plainFmt = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const plain = (v: number) => plainFmt.format(v);

/** Typed amount → cents, or null unless the whole text is a number. Strips "$", "," and spaces; accepts "−". */
function parseAmount(s: string): number | null {
  const t = String(s).replace(/[$,\s]/g, '').replace(/−/g, '-');
  if (!/^-?(\d+\.?\d*|\.\d+)$/.test(t)) return null;
  const v = Number(t);
  return Number.isFinite(v) ? round2(v) : null;
}

/** "$120.00 ready to assign." / "Every dollar assigned." / "Assigned $40.00 more than you have." */
function readyText(v: number): string {
  if (v > 0.004) return `${formatMoney(v)} ready to assign.`;
  if (v < -0.004) return `Assigned ${formatMoney(-v)} more than you have.`;
  return 'Every dollar assigned.';
}

/** The target label under a category name: "Funded", "Funded · $X extra" or "$X more needed". */
function targetProgress(c: Line) {
  const t = c.target!;
  const funded = c.under < 0.005;
  // A monthly target fills toward its amount; a by-date target toward what this month needs.
  const need = t.kind === 'monthly' ? t.amount : round2(c.assigned + c.under);
  const pct = need > 0.004 ? Math.min(100, Math.max(0, (c.assigned / need) * 100)) : 100;
  const extra = t.kind === 'monthly' ? round2(c.assigned - t.amount) : 0;
  const label = funded ? (extra > 0.004 ? `Funded · ${formatMoney(extra)} extra` : 'Funded') : `${formatMoney(c.under)} more needed`;
  return { funded, pct, extra, label };
}

export function BudgetDetailed({ focusCat, onSimple }: { focusCat?: string | null; onSimple: () => void }) {
  const toast = useToast();
  const { invalidate } = useApp();
  const uid = useId();
  const currentMonth = monthOf(new Date());
  const [month, setMonth] = useState(currentMonth);
  const { budget, review, good, bm, rv, bmRef, monthRef, busy, histLen, enqueue, snapshot, pushUndo, applyMonth, latestOf, saveAssigned, saveBody, undo } = useBudgetMonth(month);
  const cats = useApi(() => api.categories.list(), []);
  const [collapsed, setCollapsed] = useState<string[]>(() => readPref(COLLAPSED_PREF, [] as string[], isStringList));
  const [filter, setFilter] = useState<Filter>('all');
  const [selId, setSelId] = useState<string | null>(null);
  /** Assigned cells being typed in (text per category id). */
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [menuOpen, setMenuOpen] = useState(false);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [removing, setRemoving] = useState<EnvelopeLine | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [targetFor, setTargetFor] = useState<TargetSubject | null>(null);
  const [newCatOpen, setNewCatOpen] = useState(false);

  /** Typed text per category that's already been sent, so Enter followed by blur saves once. */
  const committed = useRef<Record<string, string>>({});

  // Last month's assignments, for Quick assign's "Assigned last month".
  const prevMonth = addMonths(month, -1);
  const isCurrent = bm ? bm.is_current : month === currentMonth;
  const isFuture = bm ? bm.is_future : month > currentMonth;
  // Past months are view-only (the server refuses saves before today's month).
  const readOnly = !isCurrent && !isFuture;
  const wantPrev = !readOnly && !!bm?.earliest_month && prevMonth >= bm.earliest_month;
  const prev = useApi(() => (wantPrev ? api.budgets.get(prevMonth) : Promise.resolve(null)), [wantPrev, prevMonth]);
  const lastAssigned = useMemo(() => {
    const p = prev.data;
    if (!p || p.month !== prevMonth) return null;
    // Setup's "money you already had" row isn't a plan to copy.
    const plans = p.categories.filter((c) => !c.seeded && c.category !== p.seed_category);
    return plans.length ? new Map(plans.map((c) => [c.category, c.assigned])) : null;
  }, [prev.data, prevMonth]);

  const lines = useMemo(() => (bm ? bm.categories.map(toLine) : []), [bm]);

  // Groups in their order, "Other" last; a flat budget without groups is one section.
  const sections = useMemo<Section[]>(() => {
    if (!bm) return [];
    const known = new Set(bm.groups.map((g) => g.id));
    const byKey = new Map<string, Line[]>();
    for (const c of lines) {
      const k = c.group_id !== null && known.has(c.group_id) ? String(c.group_id) : 'none';
      byKey.set(k, [...(byKey.get(k) ?? []), c]);
    }
    return [...bm.groups]
      .sort((a, b) => a.position - b.position)
      .map((g) => ({ key: String(g.id), name: g.name, lines: byKey.get(String(g.id)) ?? [] }))
      .concat([{ key: 'none', name: bm.groups.length ? 'Other' : 'Categories', lines: byKey.get('none') ?? [] }])
      .filter((s) => s.lines.length > 0);
  }, [bm, lines]);

  const ordered = useMemo(() => sections.flatMap((s) => s.lines), [sections]);
  const sel = ordered.find((c) => c.category === selId) ?? ordered[0] ?? null;
  const sectionOf = (id: string) => sections.find((s) => s.lines.some((c) => c.category === id));

  // Undo is per month (the hook clears its history): switching months starts fresh.
  useEffect(() => {
    setDrafts({});
    setMenuOpen(false);
    setFocusId(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [month]);

  // Focus (and select) a category's Assigned input once it's rendered.
  useEffect(() => {
    if (!focusId) return;
    const el = document.getElementById(`${uid}-in-${focusId}`) as HTMLInputElement | null;
    if (!el) return;
    el.focus();
    el.select();
    el.scrollIntoView({ block: 'nearest' });
    setFocusId(null);
  }, [focusId, uid, sections, collapsed, filter]);

  // `?cat=` (from Home and elsewhere): select that category and focus its Assigned amount.
  const deepLinked = useRef<string | null>(null);
  useEffect(() => {
    if (!focusCat || !bm || deepLinked.current === focusCat || month !== currentMonth) return;
    deepLinked.current = focusCat;
    if (!bm.categories.some((c) => c.category === focusCat)) {
      // E.g. a bill's category from Bills and paychecks that isn't in this month's budget.
      const name = cats.data?.find((c) => c.id === focusCat)?.name ?? bm.addable.find((c) => c.id === focusCat)?.name;
      if (name) toast.push({ tone: 'info', title: `${name} isn’t in this month’s budget yet`, body: 'Use “Add a category” to put it in.' });
      return;
    }
    setSelId(focusCat);
    setFilter('all');
    const line = bm.categories.find((c) => c.category === focusCat);
    const key = line?.group_id != null && bm.groups.some((g) => g.id === line.group_id) ? String(line.group_id) : 'none';
    setCollapsed((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : cur));
    setFocusId(focusCat);
  }, [focusCat, bm, month, currentMonth]);

  if (budget.error && !good.current) return <ErrorPanel error={budget.error} onRetry={budget.reload} />;

  const earliest = good.current?.earliest_month ?? null;
  const latest = good.current?.latest_month ?? addMonths(currentMonth, 12);
  const canPrev = earliest === null || month > earliest;
  const canNext = month < latest;

  let subtitle = '';
  if (bm?.is_current) subtitle = bm.days_left === 0 ? 'Last day of the month' : `${plural(bm.days_left, 'day')} left in the month`;
  else if (isFuture) subtitle = 'Planning ahead';
  else if (bm) subtitle = 'Past month · view only';

  // ---- saving

  function setCollapsedPref(next: string[]) {
    setCollapsed(next);
    writePref(COLLAPSED_PREF, next);
  }
  const toggleGroup = (key: string) => setCollapsedPref(collapsed.includes(key) ? collapsed.filter((k) => k !== key) : [...collapsed, key]);

  /**
   * Set one category's Assigned. Values below −carryover are raised to it; raising a category
   * stops where Ready to assign reaches $0 (the server refuses anything past that). The limits
   * use the data as it is when the save runs, after any saves queued before it.
   */
  function assign(id: string, want: number, name: string): Promise<BudgetMonth | null> {
    if (readOnly) return Promise.resolve(null);
    return saveAssigned((cur) => {
      const c = cur.categories.find((x) => x.category === id);
      if (!c) return null;
      let v = round2(Math.max(-c.carryover, want));
      const cap = round2(c.assigned + Math.max(0, cur.ready_to_assign));
      if (v > c.assigned + 0.004 && v > cap + 0.004) {
        if (cap <= c.assigned + 0.004) {
          toast.push({ tone: 'warn', title: 'Nothing is ready to assign', body: `Lower another category or move money into ${c.name} instead.` });
          return null;
        }
        toast.push({
          tone: 'warn',
          title: `${c.name} stops at ${formatMoney(cap)}`,
          body: `Only ${formatMoney(cur.ready_to_assign)} was ready to assign. Lower another category to free up more.`,
        });
        v = cap;
      }
      return { [id]: v };
    }, `Couldn’t change ${name}`);
  }

  function dropDraft(id: string, onlyIf?: string) {
    setDrafts((d) => {
      if (!(id in d) || (onlyIf !== undefined && d[id] !== onlyIf)) return d;
      const { [id]: _drop, ...rest } = d;
      return rest;
    });
  }

  function commitDraft(id: string) {
    const text = drafts[id];
    if (text === undefined || committed.current[id] === text) return;
    const c = bmRef.current?.categories.find((x) => x.category === id);
    const v = parseAmount(text);
    if (!c || v === null) return dropDraft(id);
    // Keep showing the typed amount until the save lands. An amount that matches what's on
    // screen is still sent: an earlier save may be on its way (the queue drops real no-ops).
    const shown = plain(round2(Math.max(-c.carryover, v)));
    committed.current[id] = shown;
    setDrafts((d) => ({ ...d, [id]: shown }));
    void assign(id, v, c.name).finally(() => {
      if (committed.current[id] === shown) delete committed.current[id];
      dropDraft(id, shown);
    });
  }

  function fundUnderfunded() {
    const forMonth = month;
    void enqueue(async () => {
      try {
        const snap = snapshot(await latestOf(forMonth));
        const res = await api.fundTargets(forMonth);
        applyMonth(res.month);
        if (res.funded > 0.004) pushUndo(snap);
        const name = monthName(res.month.month);
        if (res.funded <= 0.004) {
          toast.push({ tone: 'warn', title: 'Nothing to fund targets with', body: `Targets still need ${formatMoney(res.unfunded)} in ${name}, but nothing is ready to assign.` });
        } else if (res.unfunded > 0.004) {
          toast.push({ tone: 'warn', title: `Funded ${formatMoney(res.funded)} of your targets`, body: `Ready to assign ran out, so ${formatMoney(res.unfunded)} is still needed in ${name}.` });
        } else {
          toast.push({ tone: 'success', title: `Funded ${formatMoney(res.funded)}`, body: `Every target in ${name} is funded. ${readyText(res.month.ready_to_assign)}` });
        }
      } catch (err) {
        toast.push({ tone: 'error', title: 'Couldn’t fund targets', body: errorMessage(err) });
      }
    });
  }

  async function move(from: Line, to: string, amount: number): Promise<boolean> {
    let toName = 'Ready to assign';
    let missing: string | null = null;
    // One-time (Release 3.17): `moved` keeps the plans that repeat next month as they were.
    const res = await saveBody((cur) => {
      const f = cur.categories.find((c) => c.category === from.category);
      if (!f) {
        missing = from.name;
        return null;
      }
      if (to !== 'rta') {
        const t = cur.categories.find((c) => c.category === to);
        if (!t) {
          missing = 'That category';
          return null;
        }
        toName = t.name;
      }
      return oneTimeMove(cur.categories, to === 'rta' ? { [f.category]: -amount } : { [f.category]: -amount, [to]: amount });
    }, `Couldn’t move money out of ${from.name}`);
    const next = res?.next ?? null;
    if (missing) toast.push({ tone: 'error', title: 'Couldn’t move money', body: `${missing} isn’t in the budget any more.` });
    if (next) toast.push({ tone: 'success', title: `Moved ${formatMoney(amount)} to ${toName}`, body: `From ${from.name}.`, timeout: 3000 });
    return !!next;
  }

  /** Put a category in this month's budget at $0, then focus it to assign. */
  async function addToBudget(id: string, name: string) {
    const forMonth = month;
    const next = await enqueue(async () => {
      try {
        const res = await api.budgets.save(forMonth, { assigned: { [id]: 0 } });
        applyMonth(res);
        return res;
      } catch (err) {
        toast.push({ tone: 'error', title: `Couldn’t add ${name} to the budget`, body: errorMessage(err) });
        return null;
      }
    });
    if (!next) return;
    toast.push({ tone: 'success', title: `Added ${name} to the budget`, body: `Type an amount to assign in ${monthName(next.month)}.`, timeout: 3000 });
    if (next.month !== monthRef.current) return;
    // The group list is computed from the new data on the next render.
    setSelId(id);
    setFilter('all');
    const line = next.categories.find((c) => c.category === id);
    const key = line?.group_id != null && next.groups.some((g) => g.id === line.group_id) ? String(line.group_id) : 'none';
    setCollapsed((cur) => {
      if (!cur.includes(key)) return cur;
      const open = cur.filter((k) => k !== key);
      writePref(COLLAPSED_PREF, open);
      return open;
    });
    setFocusId(id);
  }

  /** "+ New category…": create a spending category, then add it to the budget. */
  async function createCategory(name: string, groupId: number | null): Promise<string | null> {
    let created;
    try {
      created = await api.categories.create({ name, kind: 'spending' });
    } catch (err) {
      return errorMessage(err);
    }
    setNewCatOpen(false);
    cats.reload();
    try {
      if (groupId !== null) await api.categories.update(created.id, { group_id: groupId });
    } catch (err) {
      toast.push({ tone: 'error', title: `Created ${created.name}, but couldn’t put it in the group`, body: errorMessage(err) });
    }
    await addToBudget(created.id, created.name);
    return null;
  }

  function openTarget(id: string) {
    const line = bm?.categories.find((c) => c.category === id);
    if (!line) return;
    const t = line.target;
    setTargetFor({ id, name: line.name, target: t ? targetOf(t) : null, carryover: line.carryover, bills: line.bills?.total ?? null });
  }

  function askRemove(c: Line) {
    if (readOnly) return;
    if (Math.abs(c.available) < 0.005) void doRemove(c);
    else setRemoving(c);
  }

  async function doRemove(c: EnvelopeLine) {
    setRemoveBusy(true);
    const forMonth = month;
    await enqueue(async () => {
      try {
        applyMonth(await api.budgets.save(forMonth, { removed: [c.category] }));
        setRemoving(null);
        invalidate();
        toast.push({
          tone: 'success',
          title: `Removed ${c.name}`,
          body:
            c.available > 0.004
              ? `${formatMoney(c.available)} went back to Ready to assign. Its spending now shows as unbudgeted.`
              : c.available < -0.004
                ? `${formatMoney(-c.available)} of overspending came out of Ready to assign.`
                : 'Its spending now shows as unbudgeted.',
        });
      } catch (err) {
        toast.push({ tone: 'error', title: `Couldn’t remove ${c.name}`, body: errorMessage(err) });
      }
    });
    setRemoveBusy(false);
  }

  // ---- render

  const counts: Record<Filter, number> = {
    all: lines.length,
    under: lines.filter((c) => c.st === 'under').length,
    over: lines.filter((c) => c.st === 'over').length,
    avail: lines.filter((c) => c.available > 0.004).length,
  };
  const shown = sections.map((s) => ({ ...s, rows: s.lines.filter((c) => matches(filter, c)) })).filter((s) => s.rows.length > 0);
  const anyOpen = sections.some((s) => !collapsed.includes(s.key));

  return (
    <div className="ba-page">
      <h1 className="sr-only">Budget</h1>
      <header className="ba-head">
        <div className="ba-month">
          <button type="button" className="ba-step" onClick={() => setMonth(addMonths(month, -1))} disabled={!canPrev} aria-label="Previous month">
            <Icon name="chevronLeft" />
          </button>
          <div className="ba-month-text">
            <div className="ba-month-name" aria-live="polite">
              {monthLong(month)}
            </div>
            <div className="ba-month-sub" title={isFuture ? 'Money you assign now is set aside from what you have today.' : undefined}>
              {subtitle || ' '}
            </div>
          </div>
          <button type="button" className="ba-step" onClick={() => setMonth(addMonths(month, 1))} disabled={!canNext} aria-label="Next month">
            <Icon name="chevronRight" />
          </button>
          {month !== currentMonth && (
            <button type="button" className="ba-ghost" onClick={() => setMonth(currentMonth)}>
              This month
            </button>
          )}
        </div>
        <ReadyBlock
          bm={bm}
          open={menuOpen}
          setOpen={setMenuOpen}
          readOnly={readOnly}
          lines={lines}
          onFund={fundUnderfunded}
          onAccounts={() => setAccountsOpen(true)}
        />
      </header>

      <div className="ba-toolbar">
        <div className="ba-filters" role="group" aria-label="Show categories">
          {(
            [
              ['all', 'All', null],
              ['under', 'Underfunded', 'var(--warn)'],
              ['over', 'Overspent', 'var(--neg)'],
              ['avail', 'Money available', 'var(--pos)'],
            ] as const
          ).map(([id, label, dot]) => (
            <button key={id} type="button" className="ba-pill" aria-pressed={filter === id} onClick={() => setFilter(id)}>
              {dot && <span className="ba-dot" style={{ background: dot }} aria-hidden="true" />}
              {label}
              <span className="ba-pill-count">{counts[id]}</span>
            </button>
          ))}
        </div>
        <div className="ba-tools">
          {busy > 0 && <span className="ba-saving">Saving…</span>}
          <button type="button" className="ba-ghost" onClick={onSimple}>
            Simple view
          </button>
          <button type="button" className="ba-ghost" onClick={undo} disabled={!histLen || readOnly}>
            <Icon name="undo" />
            Undo
          </button>
          <button
            type="button"
            className="ba-ghost"
            onClick={() => setCollapsedPref(anyOpen ? sections.map((s) => s.key) : [])}
            disabled={!sections.length}
          >
            {anyOpen ? 'Collapse all' : 'Expand all'}
          </button>
        </div>
      </div>

      <div className="ba-body">
        <section className="ba-table" aria-label={`${monthLong(month)} budget`} aria-busy={!bm || undefined}>
          <div className="ba-grid ba-colhead" aria-hidden="true">
            <span>Category</span>
            <span className="ba-r">Assigned</span>
            <span className="ba-r ba-act">Activity</span>
            <span className="ba-r">Available</span>
          </div>
          {!bm ? (
            <SkeletonRows rows={6} label="Loading budget" />
          ) : lines.length === 0 ? (
            <EmptyState kind="chart" compact title={`Nothing budgeted for ${monthName(month)}`}>
              {readOnly
                ? `No categories were in the budget in ${monthName(month)}.`
                : bm.ready_to_assign > 0.004
                  ? `You have ${formatMoney(bm.ready_to_assign)} ready to assign. Add a category under Unbudgeted spending, then give it a job.`
                  : 'Add a category under Unbudgeted spending, then give it a job.'}
            </EmptyState>
          ) : shown.length === 0 ? (
            <div className="ba-none">No categories match this filter.</div>
          ) : (
            shown.map((s) => {
              const open = !collapsed.includes(s.key);
              const gAssigned = round2(s.lines.reduce((a, c) => a + c.assigned, 0));
              const gSpent = round2(s.lines.reduce((a, c) => a + c.spent, 0));
              const gAvail = round2(s.lines.reduce((a, c) => a + c.available, 0));
              return (
                <div key={s.key}>
                  <button type="button" className="ba-grid ba-group" aria-expanded={open} aria-controls={`${uid}-g-${s.key}`} onClick={() => toggleGroup(s.key)}>
                    <span className="ba-group-name">
                      <Icon name="chevronDown" className="ba-chev" />
                      {s.name}
                    </span>
                    <span className="ba-r ba-group-num">
                      <span className="sr-only">Assigned </span>
                      {formatMoney(gAssigned)}
                    </span>
                    <span className="ba-r ba-group-num ba-act">
                      <span className="sr-only">Activity </span>
                      {formatMoney(-gSpent)}
                    </span>
                    <span className={`ba-r ba-group-avail ${gAvail < -0.004 ? 'is-neg' : gAvail > 0.004 ? 'is-pos' : ''}`}>
                      <span className="sr-only">Available </span>
                      {formatMoney(gAvail)}
                    </span>
                  </button>
                  <div id={`${uid}-g-${s.key}`} hidden={!open}>
                    {s.rows.map((c) => (
                      <CategoryRow
                        key={c.category}
                        c={c}
                        inputId={`${uid}-in-${c.category}`}
                        selected={sel?.category === c.category}
                        readOnly={readOnly}
                        seeded={c.seeded || c.category === bm?.seed_category}
                        draft={drafts[c.category]}
                        onSelect={() => setSelId(c.category)}
                        onDraft={(v) => setDrafts((d) => ({ ...d, [c.category]: v }))}
                        onCommit={() => commitDraft(c.category)}
                        onDiscard={() => dropDraft(c.category)}
                      />
                    ))}
                  </div>
                </div>
              );
            })
          )}
        </section>

        <aside className="ba-side" aria-label="Selected category">
          {bm && sel ? (
            <Inspector
              key={sel.category}
              bm={bm}
              c={sel}
              group={sectionOf(sel.category)?.name ?? ''}
              readOnly={readOnly}
              lastAssigned={lastAssigned}
              members={ordered}
              prevMonth={prevMonth}
              onAssign={(v) => void assign(sel.category, v, sel.name)}
              onMove={(to, amount) => move(sel, to, amount)}
              onTarget={() => openTarget(sel.category)}
              onRemove={() => askRemove(sel)}
              onRenamed={() => {
                budget.reload();
                cats.reload();
              }}
            />
          ) : (
            <section className="ba-card">
              {bm ? (
                <p className="ba-muted">{lines.length ? 'Pick a category to see its details.' : 'Categories you add to the budget show up here.'}</p>
              ) : (
                <SkeletonRows rows={4} label="Loading category" />
              )}
            </section>
          )}
        </aside>
      </div>

      <div className="sp-grid ba-lower">
        <UnbudgetedCard bm={bm} readOnly={readOnly} onAdd={(id, name) => void addToBudget(id, name)} onNewCategory={() => setNewCatOpen(true)} onRenamed={() => budget.reload()} />
        <ReviewCard month={month} rv={rv} isCurrent={isCurrent} isFuture={isFuture} loading={review.loading} error={review.error} onRetry={review.reload} />
      </div>

      <ConfirmDialog
        open={removing !== null}
        title={removing ? `Remove ${removing.name} from the budget?` : 'Remove category?'}
        confirmLabel="Remove"
        busy={removeBusy}
        onConfirm={() => removing && void doRemove(removing)}
        onCancel={() => setRemoving(null)}
      >
        {removing &&
          (removing.available > 0
            ? `Its ${formatMoney(removing.available)} goes back to Ready to assign. Spending in it will show as unbudgeted.`
            : `Its ${formatMoney(-removing.available)} of overspending comes out of Ready to assign. Spending in it will show as unbudgeted.`)}
      </ConfirmDialog>

      <TargetModal
        subject={targetFor}
        month={readOnly ? currentMonth : month}
        onClose={() => setTargetFor(null)}
        onSaved={(title, body) => {
          setTargetFor(null);
          cats.reload();
          budget.reload();
          toast.push({ tone: 'success', title, body, timeout: 3500 });
        }}
      />

      <NewCategoryModal
        open={newCatOpen}
        groups={bm?.groups ?? []}
        existing={cats.data?.map((c) => c.name) ?? []}
        onClose={() => setNewCatOpen(false)}
        onCreate={createCategory}
      />

      {bm && (
        <BudgetAccountsModal
          bm={bm}
          open={accountsOpen}
          onClose={() => setAccountsOpen(false)}
          onSaved={(next) => {
            applyMonth(next);
            if (next.month !== month) budget.reload();
            setAccountsOpen(false);
            invalidate();
            toast.push({ tone: 'success', title: 'Budget accounts updated', body: readyText(next.ready_to_assign) });
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Ready to assign

function ReadyBlock({
  bm,
  open,
  setOpen,
  readOnly,
  lines,
  onFund,
  onAccounts,
}: {
  bm: BudgetMonth | undefined;
  open: boolean;
  setOpen: (v: boolean) => void;
  readOnly: boolean;
  lines: Line[];
  onFund: () => void;
  onAccounts: () => void;
}) {
  const uid = useId();
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  // Close on a click outside or Esc (focus goes back to the button).
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        setOpen(false);
        btnRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', down);
    document.addEventListener('keydown', key, true);
    return () => {
      document.removeEventListener('mousedown', down);
      document.removeEventListener('keydown', key, true);
    };
  }, [open, setOpen]);

  /** Run a menu item: the menu closes, so focus goes back to the Assign button. */
  function pick(run: () => void) {
    setOpen(false);
    btnRef.current?.focus();
    run();
  }

  if (!bm) {
    return (
      <div className="ba-rta is-zero" aria-busy="true">
        <div>
          <Skeleton width={120} height={26} />
          <div className="ba-rta-label">Ready to assign</div>
        </div>
      </div>
    );
  }

  const rta = bm.ready_to_assign;
  const tone = rta > 0.004 ? 'is-pos' : rta < -0.004 ? 'is-warn' : 'is-zero';
  const label = rta > 0.004 ? 'Ready to assign' : rta < -0.004 ? 'Assigned more than you have' : 'All money assigned';

  const underAll = round2(lines.reduce((s, c) => s + c.under, 0));
  const underN = lines.filter((c) => c.under > 0.004).length;
  const included = bm.budget_accounts.filter((a) => a.included);

  const items: { key: string; label: string; sub: string; amount: ReactNode; off: boolean; run: () => void }[] = [
    {
      key: 'fund',
      label: 'Fund underfunded',
      sub: readOnly ? 'Past months are view only' : underN ? `${plural(underN, 'category', 'categories')} ${underN === 1 ? 'needs' : 'need'} money` : 'Every target is funded',
      amount: formatMoney(Math.min(underAll, Math.max(0, rta))),
      off: readOnly || underAll < 0.005 || rta < 0.005,
      run: onFund,
    },
  ];

  return (
    <div className={`ba-rta ${tone}`} ref={wrapRef}>
      <div>
        <div className="ba-rta-amt num" aria-live="polite">
          {formatMoney(rta)}
        </div>
        <div className="ba-rta-label">{label}</div>
        {rta < -0.004 && <div className="ba-rta-note">Lower a category’s Assigned amount to fix it.</div>}
        {bm.ready_pending > 0.004 && (
          <div className="ba-rta-note">Includes {formatMoney(bm.ready_pending)} still expected this month</div>
        )}
      </div>
      <button ref={btnRef} type="button" className="ba-rta-btn" aria-expanded={open} aria-controls={`${uid}-menu`} onClick={() => setOpen(!open)}>
        Assign
        <Icon name="chevronDown" />
      </button>
      {open && (
        <div className="ba-menu" id={`${uid}-menu`} role="group" aria-label="Assign">
          {items.map((q) => (
            <button key={q.key} type="button" className="ba-menu-item" disabled={q.off} onClick={() => pick(q.run)}>
              <span>
                <span className="ba-menu-label">{q.label}</span>
                <span className="ba-menu-sub">{q.sub}</span>
              </span>
              <span className="ba-menu-amt num">{q.amount}</span>
            </button>
          ))}
          <div className="ba-menu-sep" role="presentation" />
          <button type="button" className="ba-menu-item" onClick={() => pick(onAccounts)}>
            <span>
              <span className="ba-menu-label">Budget accounts</span>
              <span className="ba-menu-sub">{included.length ? `${plural(included.length, 'account')} fund Ready to assign` : 'None chosen yet'}</span>
            </span>
            <span className="ba-menu-amt num">{formatMoney(bm.cash_total)}</span>
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- table row

function CategoryRow({
  c,
  inputId,
  selected,
  readOnly,
  seeded,
  draft,
  onSelect,
  onDraft,
  onCommit,
  onDiscard,
}: {
  c: Line;
  inputId: string;
  selected: boolean;
  readOnly: boolean;
  /** Setup's "money you already had" row (the month before setup): not a plan. */
  seeded: boolean;
  draft: string | undefined;
  onSelect: () => void;
  onDraft: (v: string) => void;
  onCommit: () => void;
  onDiscard: () => void;
}) {
  const t = c.target ? targetProgress(c) : null;
  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault();
      onCommit();
      const el = e.currentTarget;
      requestAnimationFrame(() => el.select());
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onDiscard();
      const el = e.currentTarget;
      requestAnimationFrame(() => el.select());
    }
  }
  return (
    <div className={`ba-grid ba-row${selected ? ' is-selected' : ''}`} onClick={onSelect}>
      <div className="ba-name-cell">
        <div className="ba-name-line">
          <button type="button" className="ba-name" onClick={onSelect} aria-pressed={selected} title={c.name}>
            {c.name}
          </button>
          {t && <span className={`ba-tlabel ${t.funded ? 'is-pos' : 'is-warn'}`}>{t.label}</span>}
          {seeded && <span className="ba-tlabel ba-note">Money you already had</span>}
          {c.debt && <span className="ba-tlabel ba-note">Debt plan</span>}
        </div>
        {t && (
          <div className="ba-tbar" aria-hidden="true">
            <span className={t.funded ? 'is-pos' : 'is-warn'} style={{ width: `${t.pct.toFixed(1)}%` }} />
          </div>
        )}
      </div>
      {readOnly ? (
        <span className="ba-r ba-amt-static num">
          <span className="sr-only">{seeded ? 'Money you already had ' : 'Assigned '}</span>
          {plain(c.assigned)}
        </span>
      ) : (
        <input
          id={inputId}
          className="ba-amt-in num"
          inputMode="decimal"
          autoComplete="off"
          aria-label={`Assigned to ${c.name}`}
          value={draft ?? plain(c.assigned)}
          onChange={(e) => onDraft(e.target.value)}
          onBlur={onCommit}
          onKeyDown={onKey}
          onFocus={(e) => {
            onSelect();
            e.target.select();
          }}
          onClick={(e) => e.stopPropagation()}
        />
      )}
      <span className="ba-r ba-activity ba-act num">
        <span className="sr-only">Activity </span>
        {formatMoney(-c.spent)}
      </span>
      <span className="ba-avail-cell">
        <span className={`ba-avail st-${c.st} num`}>
          <span className="sr-only">Available </span>
          {formatMoney(c.available)}
        </span>
      </span>
    </div>
  );
}

// ---------------------------------------------------------------- inspector

function Inspector({
  bm,
  c,
  group,
  readOnly,
  lastAssigned,
  members,
  prevMonth,
  onAssign,
  onMove,
  onTarget,
  onRemove,
  onRenamed,
}: {
  bm: BudgetMonth;
  c: Line;
  group: string;
  readOnly: boolean;
  lastAssigned: Map<string, number> | null;
  members: Line[];
  prevMonth: string;
  onAssign: (v: number) => void;
  onMove: (to: string, amount: number) => Promise<boolean>;
  onTarget: () => void;
  onRemove: () => void;
  onRenamed: () => void;
}) {
  const uid = useId();
  const [mvAmt, setMvAmt] = useState('');
  const [mvTo, setMvTo] = useState('rta');
  const [moving, setMoving] = useState(false);

  let note: string;
  let noteTone: string;
  if (c.st === 'over') {
    note = `Overspent by ${formatMoney(-c.available)}. Move money in to cover it.`;
    noteTone = 'is-neg';
  } else if (c.st === 'under') {
    note = `${formatMoney(c.under)} more needed for the target`;
    noteTone = 'is-warn';
  } else if (c.available > 0.004) {
    noteTone = 'is-pos';
    if (bm.is_current) note = bm.days_left > 0 ? `${formatMoney(c.available / bm.days_left)} a day for ${plural(bm.days_left, 'day')}` : 'Left for today';
    else if (bm.is_future) note = `Set aside for ${monthName(bm.month)}`;
    else note = 'Rolled into the next month';
  } else {
    note = bm.is_future && c.spent <= 0.004 ? 'Nothing assigned yet' : 'Fully spent';
    noteTone = '';
  }

  // Move money: the server keeps carryover + assigned ≥ 0, so that's the most that can leave.
  const amount = parseAmount(mvAmt);
  const movable = round2(c.carryover + c.assigned);
  const target = mvTo === 'rta' ? null : members.find((m) => m.category === mvTo && m.category !== c.category);
  const toName = mvTo === 'rta' ? 'Ready to assign' : target?.name;
  const tooMuch = amount !== null && amount > movable + 0.004;
  const mvOk = amount !== null && amount > 0.004 && !tooMuch && (mvTo === 'rta' || !!target) && !moving;
  const hint = tooMuch
    ? movable > 0.004
      ? `You can move at most ${formatMoney(movable)} out of ${c.name}`
      : `${c.name} has nothing assigned to move`
    : mvOk
      ? `${formatMoney(amount)} from ${c.name} to ${toName}`
      : 'Enter an amount to move';

  async function runMove(e: FormEvent) {
    e.preventDefault();
    if (!mvOk || amount === null) return;
    setMoving(true);
    const ok = await onMove(mvTo, amount);
    setMoving(false);
    if (ok) setMvAmt('');
  }

  const last = lastAssigned?.get(c.category) ?? 0;
  const quick: { label: string; v: number; run: number; off: boolean }[] = [
    { label: 'Underfunded', v: c.under, run: round2(c.assigned + c.under), off: c.under < 0.005 },
    { label: 'Assigned last month', v: last, run: last, off: !lastAssigned || Math.abs(last - c.assigned) < 0.005 },
    { label: bm.is_current ? 'Spent this month' : `Spent in ${monthName(bm.month)}`, v: c.spent, run: c.spent, off: Math.abs(c.spent - c.assigned) < 0.005 },
    { label: 'Reset to zero', v: 0, run: 0, off: Math.abs(c.assigned) < 0.005 },
  ];

  return (
    <>
      <section className="ba-card ba-summary" aria-labelledby={`${uid}-name`}>
        <div className="ba-summary-head">
          <div className="ba-summary-title">
            <div className="ba-muted ba-xs">{group}</div>
            <h2 id={`${uid}-name`}>
              <RenameName id={c.category} name={c.name} onRenamed={onRenamed} disabled={readOnly} />
            </h2>
          </div>
          {!readOnly && (
            <button type="button" className="icon-btn icon-btn-danger" onClick={onRemove} aria-label={`Remove ${c.name} from the budget`} title="Remove from budget">
              <Icon name="trash" />
            </button>
          )}
        </div>
        <div className="ba-availbox">
          <div>
            <div className="ba-muted ba-xs">Available</div>
            <div className={`ba-xs ba-note ${noteTone}`}>{note}</div>
          </div>
          <span className={`ba-avail ba-avail-lg st-${c.st} num`}>{formatMoney(c.available)}</span>
        </div>
        <dl className="ba-facts">
          {Math.abs(c.carryover) > 0.004 && (
            <>
              <dt>Carried over from {monthName(prevMonth)}</dt>
              <dd className="num">{formatMoney(c.carryover)}</dd>
            </>
          )}
          <dt>{c.seeded || c.category === bm.seed_category ? 'Money you already had' : 'Assigned this month'}</dt>
          <dd className="num">{formatMoney(c.assigned)}</dd>
          <dt>Activity</dt>
          <dd className="num">{formatMoney(-c.spent)}</dd>
        </dl>
        {c.debt && (
          <p className="ba-xs ba-muted">
            Extra on top of your normal loan payments, set on Reports › Paying off debt. Put your extra loan payment in this category on the Transactions
            page. <Link to="/reports?tab=debt">See the plan →</Link>
          </p>
        )}
        {!readOnly && (
          <form className="ba-move" onSubmit={runMove} noValidate>
            <label className="ba-move-title" htmlFor={`${uid}-mv`}>
              Move money
            </label>
            <div className="ba-move-fields">
              <span className="ba-money">
                <span aria-hidden="true">$</span>
                <input
                  id={`${uid}-mv`}
                  className="num"
                  inputMode="decimal"
                  autoComplete="off"
                  placeholder="0.00"
                  value={mvAmt}
                  onChange={(e) => setMvAmt(e.target.value)}
                  aria-invalid={tooMuch || undefined}
                  aria-describedby={`${uid}-mvhint`}
                />
              </span>
              <select className="ba-select" value={target || mvTo === 'rta' ? mvTo : 'rta'} onChange={(e) => setMvTo(e.target.value)} aria-label="Move to">
                <option value="rta">→ Ready to assign</option>
                {members
                  .filter((m) => m.category !== c.category)
                  .map((m) => (
                    <option key={m.category} value={m.category}>
                      → {m.name}
                    </option>
                  ))}
              </select>
            </div>
            <div className="ba-move-foot">
              <span id={`${uid}-mvhint`} className={`ba-xs ${tooMuch ? 'ba-hint-err' : 'ba-muted'}`}>
                {hint}
              </span>
              <button type="submit" className="ba-move-btn" disabled={!mvOk}>
                {moving ? 'Moving…' : 'Move'}
              </button>
            </div>
          </form>
        )}
      </section>

      <TargetCard c={c} readOnly={readOnly} onTarget={onTarget} />

      {!readOnly && (
        <section className="ba-card ba-quick" aria-labelledby={`${uid}-quick`}>
          <h3 id={`${uid}-quick`}>Quick assign</h3>
          {quick.map((q) => (
            <button
              key={q.label}
              type="button"
              className="ba-quick-item"
              disabled={q.off}
              onClick={() => onAssign(q.run)}
              aria-label={`${q.label}: set ${c.name} to ${formatMoney(q.run)}`}
            >
              <span className="ba-quick-label">{q.label}</span>
              <span className="ba-quick-amt num">{formatMoney(q.v)}</span>
            </button>
          ))}
        </section>
      )}
    </>
  );
}

function TargetCard({ c, readOnly, onTarget }: { c: Line; readOnly: boolean; onTarget: () => void }) {
  const uid = useId();
  const t = c.target;
  if (!t) {
    return (
      <section className="ba-card ba-target" aria-labelledby={`${uid}-t`}>
        <div className="ba-target-head">
          <h3 id={`${uid}-t`}>Target</h3>
        </div>
        <p className="ba-muted">No target yet. A target tells you how much this category needs each month.</p>
        <button type="button" className="ba-outline" onClick={onTarget}>
          Create target
        </button>
      </section>
    );
  }

  const bills = t.kind === 'bills';
  const monthly = t.kind !== 'by_date' || !t.date;
  const amount = targetAmount(t);
  const funded = c.under < 0.005;
  let pct: number;
  let progress: string;
  let status: string;
  if (monthly) {
    pct = amount > 0 ? Math.min(100, Math.max(0, (c.assigned / amount) * 100)) : 100;
    progress = `${formatMoney(c.assigned)} of ${formatMoney(amount)} assigned`;
    const extra = round2(c.assigned - amount);
    status = funded ? (extra > 0.004 ? `${formatMoney(extra)} over target` : 'Funded') : `${formatMoney(c.under)} to go`;
  } else {
    const have = round2(c.carryover + c.assigned);
    pct = amount > 0 ? Math.min(100, Math.max(0, (have / amount) * 100)) : 100;
    progress = `${formatMoney(have)} of ${formatMoney(amount)} set aside`;
    status = funded ? (have >= amount - 0.004 ? 'Funded' : 'On track') : `${formatMoney(c.under)} needed this month`;
  }
  const tone = funded ? 'is-pos' : 'is-warn';

  return (
    <section className="ba-card ba-target" aria-labelledby={`${uid}-t`}>
      <div className="ba-target-head">
        <h3 id={`${uid}-t`}>Target</h3>
        <span className="ba-muted ba-xs">{bills ? 'Cover my bills · this month’s bills' : monthly ? 'Needed for spending · monthly' : `Savings target · by ${formatDate(t.date!)}`}</span>
      </div>
      <div className="ba-target-amt">
        <strong className="num">{formatMoney(amount)}</strong>
        <span className="ba-muted">{bills ? 'in bills this month' : monthly ? 'each month' : `by ${formatDate(t.date!)}`}</span>
      </div>
      <div className="ba-target-bar" role="img" aria-label={`${progress}. ${status}`}>
        <span className={tone} style={{ width: `${pct.toFixed(1)}%` }} />
      </div>
      <div className="ba-target-foot ba-xs">
        <span>{progress}</span>
        <span className={`ba-target-status ${tone}`}>{status}</span>
      </div>
      {!readOnly && (
        <button type="button" className="ba-outline" onClick={onTarget}>
          Edit target
        </button>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- inline rename

/** Category name with a pencil that swaps in an input: Enter or blur saves, Esc cancels. */
export function RenameName({ id, name, onRenamed, disabled, extra }: { id: string; name: string; onRenamed: () => void; disabled?: boolean; extra?: ReactNode }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(name);
  const [pending, setPending] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const settled = useRef(true);
  const shown = pending ?? name;

  // The server's name arrived (after the reload), so drop the optimistic one.
  useEffect(() => setPending(null), [name]);
  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [open]);

  function close(refocus: boolean) {
    settled.current = true;
    setOpen(false);
    if (refocus) requestAnimationFrame(() => btnRef.current?.focus());
  }

  async function commit(refocus: boolean) {
    if (settled.current) return;
    const next = value.trim().replace(/\s+/g, ' ');
    close(refocus);
    if (!next || next === shown) return;
    setPending(next);
    try {
      await api.categories.update(id, { name: next });
      onRenamed();
    } catch (err) {
      setPending(null);
      toast.push({ tone: 'error', title: 'Couldn’t rename the category', body: errorMessage(err) });
    }
  }

  if (open) {
    return (
      <input
        ref={inputRef}
        className="input env-rename-input"
        value={value}
        maxLength={60}
        aria-label={`New name for ${shown}`}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            void commit(true);
          } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            close(true);
          }
        }}
        onBlur={() => void commit(false)}
      />
    );
  }
  return (
    <span className="env-name-line">
      <span className="env-name-text" title={shown}>
        {shown}
      </span>
      {!disabled && (
        <button
          ref={btnRef}
          type="button"
          className="icon-btn env-rename"
          aria-label={`Rename ${shown}`}
          title="Rename"
          onClick={() => {
            setValue(shown);
            settled.current = false;
            setOpen(true);
          }}
        >
          <Icon name="pencil" />
        </button>
      )}
      {extra}
    </span>
  );
}

// ---------------------------------------------------------------- unbudgeted spending

/** Option value for "+ New category…" (category ids never start with a double underscore). */
const NEW_CATEGORY = '__new__';

function UnbudgetedCard({
  bm,
  readOnly,
  onAdd,
  onNewCategory,
  onRenamed,
}: {
  bm: BudgetMonth | undefined;
  readOnly: boolean;
  onAdd: (id: string, name: string) => void;
  onNewCategory: () => void;
  onRenamed: () => void;
}) {
  const headId = useId();
  const selectId = useId();
  if (!bm) {
    return (
      <section className="panel unb-card" aria-labelledby={headId} aria-busy="true">
        <div className="panel-head">
          <h2 id={headId}>Unbudgeted spending</h2>
        </div>
        <SkeletonRows rows={3} label="Loading unbudgeted spending" />
      </section>
    );
  }
  const list = bm.unbudgeted;
  const total = round2(list.reduce((a, u) => a + u.spent, 0));
  const addable = bm.addable;
  const name = monthName(bm.month);

  return (
    <section className="panel unb-card" aria-labelledby={headId}>
      <div className="panel-head">
        <h2 id={headId}>Unbudgeted spending</h2>
        {total > 0.004 && <span className="num unb-total">{formatMoney(total)}</span>}
      </div>
      <p className="unb-intro">
        {list.length
          ? 'Spent from your budget accounts in categories that aren’t in the budget. It still comes out of your money.'
          : bm.is_future
            ? `Nothing spent in ${name} yet.`
            : `Everything spent from your budget accounts in ${name} is in the budget.`}
      </p>
      {list.length > 0 && (
        <ul className="unb-list">
          {list.map((u) => (
            <li className={`unb-row${readOnly ? ' is-readonly' : ''}`} key={u.category}>
              <span className="swatch" style={{ background: segmentColor(u.hue) } as CSSProperties} aria-hidden="true" />
              <div className="unb-name">
                <RenameName id={u.category} name={u.name} onRenamed={onRenamed} />
              </div>
              <span className="num unb-amt">{formatMoney(u.spent)}</span>
              {!readOnly && (
                <button type="button" className="btn btn-sm" onClick={() => onAdd(u.category, u.name)} aria-label={`Add ${u.name} to the budget`}>
                  <Icon name="plus" />
                  Add to budget
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {!readOnly && (
        <div className="alloc-add unb-add">
          <label htmlFor={selectId} className="sr-only">
            Add a category to the budget
          </label>
          <select
            id={selectId}
            className="select"
            value=""
            onChange={(e) => {
              if (e.target.value === NEW_CATEGORY) onNewCategory();
              else {
                const a = addable.find((x) => x.id === e.target.value);
                if (a) onAdd(a.id, a.name);
              }
            }}
          >
            <option value="">Add a category…</option>
            {addable.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
            <option value={NEW_CATEGORY}>+ New category…</option>
          </select>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- targets

const inMonths = (n: number) => {
  const d = new Date();
  d.setMonth(d.getMonth() + n);
  return toISODate(d);
};

/** Set, change or clear a category's target: an amount every month, or an amount by a date. */
export function TargetModal({
  subject,
  month,
  onClose,
  onSaved,
}: {
  subject: TargetSubject | null;
  /** The month the "a month" preview is counted from (current or future). */
  month: string;
  onClose: () => void;
  onSaved: (title: string, body: string) => void;
}) {
  const uid = useId();
  const [kind, setKind] = useState<CategoryTarget['kind']>('monthly');
  const [amount, setAmount] = useState('');
  const [date, setDate] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ field: 'amount' | 'date' | 'form'; msg: string } | null>(null);
  const amountRef = useRef<HTMLInputElement>(null);
  const today = toISODate(new Date());

  useEffect(() => {
    if (!subject) return;
    const t = subject.target;
    setKind(t?.kind ?? 'monthly');
    setAmount(t && t.kind !== 'bills' ? str(t.amount) : '');
    setDate(t?.date ?? inMonths(6));
    setError(null);
    const h = requestAnimationFrame(() => amountRef.current?.focus());
    return () => cancelAnimationFrame(h);
  }, [subject]);

  const value = parseMoneyInput(amount);
  const valid = value !== null && Number.isFinite(value) && value > 0;
  let preview: string | null = null;
  if (subject && kind === 'bills') {
    preview =
      subject.bills != null && subject.bills > 0.004
        ? `Plan what ${subject.name}’s bills add up to each month: ${formatMoney(subject.bills)} in ${monthName(month)}.`
        : `Plan what ${subject.name}’s bills add up to each month. Add bills in Bills and paychecks with this category.`;
  } else if (subject && valid) {
    if (kind === 'monthly') preview = `Assign ${formatMoney(value)} to ${subject.name} every month.`;
    else if (date && date >= today) {
      const per = targetNeeded({ kind: 'by_date', amount: value, date }, subject.carryover, 0, month);
      preview =
        date.slice(0, 7) <= month
          ? `Have ${formatMoney(value)} available in ${subject.name} by ${formatDate(date)}.`
          : `About ${formatMoney(per)} a month from ${monthName(month)} to have ${formatMoney(value)} by ${formatDate(date)}.`;
    }
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!subject || busy) return;
    if (kind === 'bills') {
      setBusy(true);
      setError(null);
      try {
        await api.categories.update(subject.id, { target: { kind: 'bills' } });
        onSaved(`Target set for ${subject.name}`, 'It covers this category’s bills each month.');
      } catch (err) {
        setError({ field: 'form', msg: errorMessage(err) });
      } finally {
        setBusy(false);
      }
      return;
    }
    if (!valid) return setError({ field: 'amount', msg: 'Enter an amount more than $0.' });
    if (kind === 'by_date') {
      if (!date) return setError({ field: 'date', msg: 'Pick the date you need the money by.' });
      if (date < today) return setError({ field: 'date', msg: 'Pick a date today or later.' });
    }
    setBusy(true);
    setError(null);
    try {
      await api.categories.update(subject.id, { target: kind === 'monthly' ? { kind, amount: value } : { kind, amount: value, date } });
      onSaved(`Target set for ${subject.name}`, kind === 'monthly' ? `${formatMoney(value)} a month.` : `${formatMoney(value)} by ${formatDate(date)}.`);
    } catch (err) {
      setError({ field: 'form', msg: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    if (!subject || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.categories.update(subject.id, { target: null });
      onSaved(`Target removed from ${subject.name}`, 'Its assigned money stays where it is.');
    } catch (err) {
      setError({ field: 'form', msg: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  const formId = `${uid}-form`;
  return (
    <Modal
      open={subject !== null}
      width={460}
      title={subject ? (subject.target ? `Target for ${subject.name}` : `Set a target for ${subject.name}`) : 'Target'}
      subtitle="Iron Owl shows what to assign each month to stay on track, and Fund targets can assign it for you."
      onClose={onClose}
      busy={busy}
      footer={
        <>
          {subject?.target && (
            <button type="button" className="btn btn-ghost btn-danger-quiet foot-start" onClick={() => void clear()} disabled={busy}>
              <Icon name="trash" />
              Remove target
            </button>
          )}
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form={formId} className="btn btn-primary" disabled={busy}>
            {busy ? 'Saving…' : 'Save target'}
          </button>
        </>
      }
    >
      <form id={formId} className="modal-form target-form" onSubmit={save} noValidate>
        <div className="field">
          <span className="field-label" id={`${uid}-kind`}>
            Target
          </span>
          <div className="segmented a-seg" role="group" aria-labelledby={`${uid}-kind`}>
            <button type="button" aria-pressed={kind === 'monthly'} onClick={() => setKind('monthly')}>
              Every month
            </button>
            <button type="button" aria-pressed={kind === 'by_date'} onClick={() => setKind('by_date')}>
              By a date
            </button>
            <button type="button" aria-pressed={kind === 'bills'} onClick={() => setKind('bills')}>
              Cover my bills
            </button>
          </div>
          {kind === 'bills' && <p className="field-hint">The total of this category’s bills each month, from Bills and paychecks.</p>}
        </div>
        {kind !== 'bills' && (
        <div className="a-form-grid">
          <div className="field">
            <label className="field-label" htmlFor={`${uid}-amt`}>
              {kind === 'monthly' ? 'Amount each month' : 'Amount needed'}
            </label>
            <span className="money-input">
              <span className="money-prefix" aria-hidden="true">
                $
              </span>
              <input
                ref={amountRef}
                id={`${uid}-amt`}
                className="input"
                inputMode="decimal"
                autoComplete="off"
                value={amount}
                onChange={(e) => {
                  setAmount(e.target.value);
                  setError(null);
                }}
                aria-invalid={error?.field === 'amount' || undefined}
                aria-describedby={error?.field === 'amount' ? `${uid}-err` : undefined}
              />
            </span>
          </div>
          {kind === 'by_date' && (
            <div className="field">
              <label className="field-label" htmlFor={`${uid}-date`}>
                Needed by
              </label>
              <input
                id={`${uid}-date`}
                className="input"
                type="date"
                min={today}
                value={date}
                onChange={(e) => {
                  setDate(e.target.value);
                  setError(null);
                }}
                aria-invalid={error?.field === 'date' || undefined}
                aria-describedby={error?.field === 'date' ? `${uid}-err` : undefined}
              />
            </div>
          )}
        </div>
        )}
        {error && (
          <p className="field-error" id={`${uid}-err`} role="alert">
            {error.msg}
          </p>
        )}
        {preview && !error && (
          <p className="goal-preview" aria-live="polite">
            <Icon name="target" />
            <span>{preview}</span>
          </p>
        )}
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- new category

function NewCategoryModal({
  open,
  groups,
  existing,
  onClose,
  onCreate,
}: {
  open: boolean;
  groups: BudgetMonth['groups'];
  /** Names already taken (case-insensitive). */
  existing: string[];
  onClose: () => void;
  /** Resolves to an error message, or null once created. */
  onCreate: (name: string, groupId: number | null) => Promise<string | null>;
}) {
  const uid = useId();
  const [name, setName] = useState('');
  const [group, setGroup] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setName('');
    setGroup('');
    setError(null);
    const h = requestAnimationFrame(() => nameRef.current?.focus());
    return () => cancelAnimationFrame(h);
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const n = name.trim().replace(/\s+/g, ' ');
    if (!n) return setError('Give the category a name.');
    if (existing.some((x) => x.toLowerCase() === n.toLowerCase())) return setError(`There’s already a category called “${n}”.`);
    setBusy(true);
    setError(null);
    const err = await onCreate(n, group ? Number(group) : null);
    setBusy(false);
    if (err) setError(err);
  }

  const formId = `${uid}-form`;
  const sorted = [...groups].sort((a, b) => a.position - b.position);
  return (
    <Modal
      open={open}
      width={440}
      title="New spending category"
      subtitle="It’s added to this month’s budget so you can assign money to it."
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form={formId} className="btn btn-primary" disabled={busy}>
            <Icon name="plus" />
            {busy ? 'Adding…' : 'Add to budget'}
          </button>
        </>
      }
    >
      <form id={formId} className="modal-form a-form-grid" onSubmit={submit} noValidate>
        <div className="field">
          <label className="field-label" htmlFor={`${uid}-name`}>
            Name
          </label>
          <input
            ref={nameRef}
            id={`${uid}-name`}
            className="input"
            value={name}
            maxLength={60}
            autoComplete="off"
            placeholder="e.g. Gifts"
            onChange={(e) => {
              setName(e.target.value);
              setError(null);
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${uid}-err` : undefined}
          />
        </div>
        {sorted.length > 0 && (
          <div className="field">
            <label className="field-label" htmlFor={`${uid}-group`}>
              Group
            </label>
            <select id={`${uid}-group`} className="select" value={group} onChange={(e) => setGroup(e.target.value)}>
              <option value="">Other (no group)</option>
              {sorted.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>
        )}
        {error && (
          <p className="field-error a-span" id={`${uid}-err`} role="alert">
            {error}
          </p>
        )}
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- budget accounts

export function BudgetAccountsModal({ bm, open, onClose, onSaved }: { bm: BudgetMonth; open: boolean; onClose: () => void; onSaved: (next: BudgetMonth) => void }) {
  const toast = useToast();
  const [sel, setSel] = useState<Set<number>>(() => new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) setSel(new Set(bm.budget_accounts.filter((a) => a.included).map((a) => a.id)));
    // Reset only when (re)opened, not on every refresh of the month.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const total = round2(bm.budget_accounts.filter((a) => sel.has(a.id)).reduce((s, a) => s + a.balance, 0));

  async function save() {
    setBusy(true);
    try {
      onSaved(await api.budgets.setAccounts([...sel]));
    } catch (err) {
      toast.push({ tone: 'error', title: 'Couldn’t update budget accounts', body: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="Budget accounts"
      subtitle="Ready to assign is the money in these accounts, minus what you’ve already assigned."
      onClose={onClose}
      busy={busy}
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={() => void save()} disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </>
      }
    >
      {bm.budget_accounts.length === 0 ? (
        <p className="small muted">No bank, credit card or other accounts yet. Add one on the Accounts page.</p>
      ) : (
        <fieldset className="acct-picks">
          <legend className="sr-only">Accounts that fund the budget</legend>
          {bm.budget_accounts.map((a) => (
            <label className="check acct-pick" key={a.id}>
              <input
                type="checkbox"
                checked={sel.has(a.id)}
                onChange={(e) =>
                  setSel((s) => {
                    const n = new Set(s);
                    if (e.target.checked) n.add(a.id);
                    else n.delete(a.id);
                    return n;
                  })
                }
              />
              <span className="acct-pick-main">
                <span className="acct-pick-name">
                  {a.name}
                  {a.mask && <span className="subtle"> ··{a.mask}</span>}
                </span>
                <span className="xsmall subtle">{[a.institution_name, CATEGORY_SINGULAR[a.category]].filter(Boolean).join(' · ')}</span>
              </span>
              <Money value={a.balance} tone={a.balance < 0 ? 'liab' : undefined} className="acct-pick-bal" />
            </label>
          ))}
        </fieldset>
      )}
      <p className="acct-note">
        <Icon name="info" />
        Credit cards count as money owed, so their balances are subtracted. Spending on a card already came out of your categories.
      </p>
      <div className="acct-total">
        <span>Total in budget accounts</span>
        <Money value={total} className="strong" />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- review

function ReviewCard({
  month,
  rv,
  isCurrent,
  isFuture,
  loading,
  error,
  onRetry,
}: {
  month: string;
  rv: SpendingReview | undefined;
  isCurrent: boolean;
  isFuture: boolean;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const headId = useId();
  return (
    <section className="panel review-card" aria-labelledby={headId}>
      <div className="panel-head">
        <h2 id={headId}>{monthName(month)} review</h2>
        <span className="xsmall subtle">{isCurrent ? 'Month to date' : isFuture ? 'Not started' : 'Full month'}</span>
      </div>
      {!rv ? (
        error && !loading ? (
          <div className="panel-body">
            <p className="small muted">{errorMessage(error)}</p>
            <button type="button" className="btn btn-sm" style={{ marginTop: 8 }} onClick={onRetry}>
              <Icon name="sync" />
              Try again
            </button>
          </div>
        ) : (
          <SkeletonRows rows={5} label="Loading review" />
        )
      ) : (
        <ReviewBody rv={rv} isFuture={isFuture} />
      )}
    </section>
  );
}

function ReviewBody({ rv, isFuture }: { rv: SpendingReview; isFuture: boolean }) {
  const delta = rv.compare.spent - rv.compare.prev_spent;
  return (
    <>
      <dl className="review-sums">
        <dt>Income</dt>
        <dd>
          <Money value={rv.income} tone="in" signed />
        </dd>
        <dt>Fixed costs</dt>
        <dd>
          <Money value={-rv.fixed} />
        </dd>
        <dt>Budgeted spending</dt>
        <dd>
          <Money value={-rv.budgeted_spending} />
        </dd>
        {rv.other_spending > 0.004 && (
          <>
            <dt>Other spending</dt>
            <dd>
              <Money value={-rv.other_spending} />
            </dd>
          </>
        )}
        <dt className="review-total">Left over</dt>
        <dd className="review-total">
          <Money value={rv.left_over} />
          {rv.left_over_pct !== null && <span className="muted"> · {Math.round(rv.left_over_pct)}% of income</span>}
        </dd>
      </dl>

      <div className="review-facts">
        {isFuture ? (
          // Comparing a month that hasn't started would only compare against nothing.
          <div>
            <span className="review-not-started">This month hasn’t started</span>
          </div>
        ) : (
          <>
            <div>
              <span className="muted">Compared with {monthName(rv.compare.prev_month)}</span>
              {rv.compare.prev_spent > 0.004 ? (
                <span className={`num strong ${delta > 0.004 ? 'neg' : delta < -0.004 ? 'pos' : ''}`}>
                  {formatMoney(delta, { signed: true })}
                  {rv.compare.delta_pct !== null && ` (${rv.compare.delta_pct > 0 ? '+' : rv.compare.delta_pct < 0 ? '−' : ''}${Math.abs(rv.compare.delta_pct).toFixed(1)}%)`}
                </span>
              ) : (
                <span className="subtle">No spending that month</span>
              )}
            </div>
            <div>
              <span className="muted">Biggest category</span>
              <span className="strong truncate">{rv.biggest ? `${rv.biggest.name} · ${formatMoney(rv.biggest.spent)}` : '—'}</span>
            </div>
            <div>
              <span className="muted">Most visited</span>
              <span className="strong truncate">
                {rv.most_visited ? `${rv.most_visited.merchant} · ${rv.most_visited.count}× · ${formatMoney(rv.most_visited.spent)}` : '—'}
              </span>
            </div>
          </>
        )}
      </div>

      <MonthChart history={rv.history} />
    </>
  );
}

/** 6-month bar chart; value and month labels are HTML positioned over the SVG. */
function MonthChart({ history }: { history: SpendingReview['history'] }) {
  const W = 420;
  const Hh = 150;
  const H = 118;
  const top = 14;
  const last = history[history.length - 1];
  const budgetLine = last?.assigned ?? 0;
  const maxV = niceCeil(Math.max(budgetLine, ...history.map((h) => h.spent), 1) * 1.12);
  const n = history.length || 1;
  const barW = 44;
  // Design: x = 12 + i·70 for six bars (12px left inset, 14px right).
  const slot = n > 1 ? (W - 12 - 14 - barW) / (n - 1) : 0;
  const bars = history.map((h, i) => {
    const bh = Math.max(h.spent > 0 ? 2 : 0, (h.spent / maxV) * H);
    const x = n > 1 ? 12 + i * slot : (W - barW) / 2;
    return { ...h, x, h: bh, y: top + H - bh, cx: x + barW / 2, current: i === history.length - 1 };
  });
  const budgetY = top + H - (Math.min(budgetLine, maxV) / maxV) * H;
  const pos = (x: number, y: number) => ({ left: `${((x / W) * 100).toFixed(2)}%`, top: `${((y / Hh) * 100).toFixed(2)}%` });
  const compact = (v: number) => (v >= 1000 ? `$${(v / 1000).toFixed(1)}K` : formatMoney(v, { cents: false }));
  const label = history.map((h) => `${monthShort(h.month)} ${formatMoney(h.spent, { cents: false })}`).join(', ');

  return (
    <div className="review-chart">
      <div className="review-chart-head">
        <h3>Spending by month</h3>
        {budgetLine > 0 && (
          <span className="legend-budget">
            <span aria-hidden="true" />
            Assigned
          </span>
        )}
      </div>
      <div className="bar-chart" role="img" aria-label={`Spending by month: ${label}${budgetLine > 0 ? `. Assigned ${formatMoney(budgetLine, { cents: false })}` : ''}`}>
        <svg viewBox={`0 0 ${W} ${Hh}`} aria-hidden="true" focusable="false">
          {bars.map((b) => (
            <rect key={b.month} x={b.x} y={b.y} width={barW} height={b.h} rx={4} className={b.current ? 'bar-current' : 'bar-past'} />
          ))}
          {budgetLine > 0 && <line x1={0} x2={W} y1={budgetY} y2={budgetY} className="bar-budget" />}
        </svg>
        {bars.map((b) => (
          <span key={`v${b.month}`} className="bar-label bar-value" style={pos(b.cx, b.y - 5)} aria-hidden="true">
            {compact(b.spent)}
          </span>
        ))}
        {bars.map((b) => (
          <span key={`m${b.month}`} className="bar-label bar-month" style={pos(b.cx, 146)} aria-hidden="true">
            {monthShort(b.month)}
          </span>
        ))}
      </div>
    </div>
  );
}

function niceCeil(v: number): number {
  const e = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / e;
  const step = f <= 1 ? 1 : f <= 1.2 ? 1.2 : f <= 1.5 ? 1.5 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 3 ? 3 : f <= 4 ? 4 : f <= 5 ? 5 : f <= 6 ? 6 : f <= 8 ? 8 : 10;
  return step * e;
}
