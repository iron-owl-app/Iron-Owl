import { useEffect, useId, useRef, useState, type CSSProperties, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import type { EnvelopeLine } from '../../api';
import { Icon, type IconName } from '../../components/Icon';
import { formatMoney, plural } from '../../lib/format';
import { allBillsPaid, billStatusText, billSummary, moneyShort, planStatus, round2, targetLabel } from '../../lib/budget';
import { groupWorkspaceTotals, isReservedLine, parsePlan } from './workspaceMath';
import { BudgetCategoryPanel } from './BudgetCategoryPanel';

/*
 * Simple view: the selected group's category workspace. The group's decorative hue comes
 * from its position (lib/categoryColors groupHueAt), passed in as `hue`.
 */

// Re-exported so existing imports keep working; the parser lives with the tested math.
export { parsePlan };

export interface Section {
  key: string;
  /** null for categories without a group. */
  groupId: number | null;
  name: string;
  hue: number;
  lines: EnvelopeLine[];
}

/** What a category card or its options can ask the page to do. */
export interface RowActions {
  onPlan: (c: EnvelopeLine, value: number) => Promise<string | null>;
  onCover: (c: EnvelopeLine, el: HTMLElement) => void;
  /** "Plan $245": this month's plan becomes the bill total. */
  onPlanBills: (c: EnvelopeLine) => void;
  /** "Always plan for my bills": a `bills` target, then this month's plan (one Undo). */
  onAlwaysBills: (c: EnvelopeLine) => void;
  /** "+ Add a bill": the calendar's Add dialog with this category chosen. */
  onAddBill: (c: EnvelopeLine) => void;
  onTarget: (c: EnvelopeLine) => void;
  onMove: (c: EnvelopeLine) => void;
  /** Resolves to an error message, or null once renamed. */
  onRename: (c: EnvelopeLine, name: string) => Promise<string | null>;
  /** Out of this month's plan only (the category stays). */
  onRemove: (c: EnvelopeLine) => void;
  /** Deleted everywhere, with Undo; only categories the user made (`c.custom`). */
  onDelete: (c: EnvelopeLine) => void;
}

export function GroupCard({
  section,
  savingsId,
  showCarry,
  monthName,
  prevMonthName,
  today,
  notPlanned,
  readyToAssign,
  monthMoney,
  adding,
  flashId,
  openId,
  onToggle,
  onStartAdd,
  onCancelAdd,
  onAdd,
  actions,
  readOnly = false,
  onRegisterEditor,
  onMoveMoney,
  onSeeTransactions,
  month,
  transactionScopeKey,
  onDeleteGroup,
}: {
  section: Section;
  savingsId: string | null;
  showCarry: boolean;
  /** "September" (the viewed month). */
  monthName: string;
  prevMonthName: string;
  /** Today, YYYY-MM-DD (the calendar link opens on the next bill from today). */
  today: string;
  notPlanned: number;
  /** Raw Ready to Assign (negative when over-planned) and the month's money, for the plan hint. */
  readyToAssign: number;
  monthMoney: number;
  adding: boolean;
  flashId: string | null;
  /** The category whose details panel is open (one at a time on the page). */
  openId: string | null;
  onToggle: (id: string) => void;
  onStartAdd: () => void;
  onCancelAdd: () => void;
  /** Resolves to an error message, or null once added. */
  onAdd: (name: string, plan: number) => Promise<string | null>;
  actions: RowActions;
  readOnly?: boolean;
  onRegisterEditor?: (guard: (() => Promise<boolean>) | null) => void;
  onMoveMoney?: () => void;
  onSeeTransactions?: () => void;
  month?: string;
  transactionScopeKey?: string;
  /** Deletes the group; its categories stay, without a group. Absent for "Not in a group". */
  onDeleteGroup?: () => void;
}) {
  const headId = useId();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const deleteGroupBtn = useRef<HTMLButtonElement>(null);
  const keepGroup = () => { setConfirmDelete(false); deleteGroupBtn.current?.focus(); };
  const totals = groupWorkspaceTotals(section.lines, savingsId);
  // Totals reconcile the actual signed assignments, including withdrawals from savings.
  const assigned = section.lines.reduce((sum, c) => sum + Math.round(c.assigned * 100), 0) / 100;
  const selected = section.lines.find((c) => c.category === openId);
  const n = section.lines.length;
  return (
    <section className="bud-group bud-workspace-cards" style={{ '--h': section.hue } as CSSProperties} aria-labelledby={headId}>
      <header className="bud-group-head">
        <span className="bud-group-title">
          <span className="bud-group-letter" aria-hidden="true">
            {section.name.slice(0, 1).toUpperCase()}
          </span>
          <span>
            <h2 id={headId} className="bud-group-name">
              {section.name}
            </h2>
            <span className="bud-group-count">{n ? plural(n, 'category', 'categories') : 'No categories yet'}</span>
          </span>
        </span>
        {!readOnly && <div className="bud-group-actions">
          {onMoveMoney && <button type="button" className="bud-btn" onClick={onMoveMoney}><Icon name="arrows" />Move money</button>}
          <button type="button" className="bud-btn" onClick={onStartAdd}><Icon name="plus" />Add category</button>
          {onDeleteGroup && <button ref={deleteGroupBtn} type="button" className="bud-btn" aria-expanded={confirmDelete} onClick={() => setConfirmDelete((v) => !v)}><Icon name="trash" />Delete group</button>}
        </div>}
      </header>
      {confirmDelete && onDeleteGroup && !readOnly && <div className="bud-details-confirm bud-group-delete" role="group" aria-labelledby={`${headId}-del`}
        onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); keepGroup(); } }}>
        <p id={`${headId}-del`}>
          Delete the {section.name} group? Its categories stay in your budget, without a group. You can undo right after.
        </p>
        <div className="bud-row-btns">
          <button type="button" className="bud-btn bud-btn-sm bud-btn-primary" onClick={() => { setConfirmDelete(false); onDeleteGroup(); }}>Delete {section.name}</button>
          <button type="button" className="bud-btn bud-btn-sm" onClick={keepGroup} autoFocus>Keep it</button>
        </div>
      </div>}
      {n > 0 && <div className="bud-category-summary">
        <span><strong className={`num${totals.left < -0.004 ? ' is-warn' : ''}`}>{formatMoney(totals.left)}</strong> left in this group{Math.abs(totals.reserved) > 0.004 ? ` · ${formatMoney(totals.reserved)} reserved` : ''}</span>
        {totals.overCount > 0 && <span className="is-warn"><Icon name="alert" />{plural(totals.overCount, 'category', 'categories')} over plan</span>}
      </div>}
      {n > 0 && <div className={`bud-category-workspace${selected ? ' has-panel' : ''}`}>
        <div className="bud-category-main">
          <div className="bud-category-grid">
            {section.lines.map((c) => <CategoryCard
              key={c.category}
              c={c}
              hue={c.hue}
              savingsId={savingsId}
              prevMonthName={prevMonthName}
              flash={flashId === c.category}
              open={openId === c.category}
              onToggle={() => onToggle(c.category)}
              actions={actions}
              readOnly={readOnly}
            />)}
          </div>
        </div>
        {selected && month && <BudgetCategoryPanel
          key={`${month}:${selected.category}`}
          c={selected}
          month={month}
          monthName={monthName}
          transactionScopeKey={transactionScopeKey}
          savingsId={savingsId}
          readOnly={readOnly}
          readyToAssign={readyToAssign}
          monthMoney={monthMoney}
          onPlan={actions.onPlan}
          onClose={() => onToggle(selected.category)}
          onRegisterEditor={onRegisterEditor}
          options={<>
            <BillsSuggestion c={selected} savingsId={savingsId} actions={actions} readOnly={readOnly} />
            <RowDetails
              id={`bud-options-${selected.category}`}
              c={selected}
              savingsId={savingsId}
              monthName={monthName}
              prevMonthName={prevMonthName}
              today={today}
              actions={actions}
              readOnly={readOnly}
              month={month}
              onClose={() => onToggle(selected.category)}
            />
          </>}
          removeTools={readOnly ? null : <RemoveOrDelete c={selected} savingsId={savingsId} monthName={monthName} actions={actions} />}
        />}
      </div>}
      {!n && <p className="bud-group-empty">No categories in this group yet.{readOnly ? '' : ' Add your first category to begin its plan.'}</p>}
      {n > 0 && <div className="bud-category-totals">
        <strong>Group total</strong>
        <span>Planned <strong className="num">{formatMoney(assigned)}</strong></span>
        {showCarry && <span>From last month <strong className="num">{formatMoney(round2(section.lines.reduce((sum, c) => sum + c.carryover, 0)))}</strong></span>}
        <span>Spent <strong className="num">{formatMoney(totals.spent)}</strong></span>
        <span>Left <strong className={`num${totals.left < -0.004 ? ' is-warn' : ''}`}>{formatMoney(totals.left)}</strong></span>
      </div>}
      {!readOnly && adding ? (
        <AddCategoryForm groupName={section.name} notPlanned={notPlanned} onCancel={onCancelAdd} onAdd={onAdd} />
      ) : !readOnly && !n ? (
        <button type="button" className="bud-add-row" onClick={onStartAdd}>
          <Icon name="plus" />
          Add a category to {section.name}
        </button>
      ) : null}
      <footer className="bud-table-footer">
        <span>{readOnly ? 'Past month · changes are read-only.' : 'Click a category to see transactions and change its plan. Money left carries into next month.'}</span>
        {onSeeTransactions && <button type="button" className="bud-link-btn" onClick={onSeeTransactions}>See transactions <span aria-hidden="true">→</span></button>}
      </footer>
    </section>
  );
}

/** Money "Use savings" took out of Emergency savings this month (a negative plan), else 0. */
function usedFromSavings(c: EnvelopeLine, savingsId: string | null): number {
  return c.category === savingsId && c.assigned < -0.004 ? round2(-c.assigned) : 0;
}

/** "Includes $X left from August" and friends (the leftover a category started the month with). */
function carryNote(c: EnvelopeLine, savingsId: string | null, prevMonthName: string): string | null {
  const used = usedFromSavings(c, savingsId);
  if (used > 0) {
    return c.carryover > 0.004
      ? `Used ${formatMoney(used)} this month, of ${formatMoney(c.carryover)} you already had`
      : `Used ${formatMoney(used)} this month`;
  }
  if (c.carryover > 0.004) {
    return c.category === savingsId
      ? `Includes ${formatMoney(c.carryover)} you already had`
      : `Includes ${formatMoney(c.carryover)} left from ${prevMonthName}`;
  }
  return null;
}

/** A goal's category (Goals page): "Goal · $350 of $600 saved" (design D8 "Connection to Budget"). */
function goalNote(c: EnvelopeLine): string | null {
  const g = c.goal;
  if (!g) return null;
  return g.reached ? `Goal reached · ${moneyShort(g.saved)} saved` : `Goal · ${moneyShort(g.saved)} of ${moneyShort(g.target)} saved`;
}

/** Reports › Paying off debt put this line in the Budget (Release 3.10). */
const DEBT_NOTE = 'Put your extra loan payment in this category on the Transactions page.';
function debtNote(c: EnvelopeLine): string | null {
  return c.debt ? 'Extra debt payment · from Reports' : null;
}

/** The collapsed row's note: a goal's progress, else the bills summary ("2 bills · next due Sep 28", "Paid Sep 1"). */
function rowNote(c: EnvelopeLine, savingsId: string | null, prevMonthName: string): string | null {
  return goalNote(c) ?? debtNote(c) ?? billSummary(c.bills) ?? carryNote(c, savingsId, prevMonthName);
}

/** Bills cost more than this month's plan: offer to plan them (design 3.8). */
export function billsShortfall(c: EnvelopeLine, savingsId: string | null): number {
  if (!c.bills || c.category === savingsId || c.goal || c.debt) return 0;
  const gap = round2(c.bills.total - c.assigned);
  return gap > 0.004 ? gap : 0;
}

/** Decorative category identity; financial rules never depend on a name or icon. */
function categoryIcon(c: EnvelopeLine, savingsId: string | null): IconName {
  if (c.goal || c.category === savingsId) return 'target';
  if (c.debt) return 'wallet';
  const name = c.name.toLowerCase();
  if (/grocery|groceries|food|eating|dining|restaurant/.test(name)) return 'basket';
  if (/rent|mortgage|home/.test(name)) return 'house';
  if (/utilit|electric|water|energy/.test(name)) return 'bolt';
  if (/personal|health|care|medical/.test(name)) return 'heart';
  if (/invest|trading/.test(name)) return 'trend';
  if (/game|fun|entertain|movie|gift/.test(name)) return 'sparkle';
  return c.bills ? 'receipt' : 'box';
}

function CategoryCard({
  c, hue, savingsId, prevMonthName, flash, open, onToggle, actions, readOnly,
}: {
  c: EnvelopeLine;
  hue: number;
  savingsId: string | null;
  prevMonthName: string;
  flash: boolean;
  open: boolean;
  onToggle: () => void;
  actions: RowActions;
  readOnly: boolean;
}) {
  const s = planStatus(c, { savingsId, hasBills: allBillsPaid(c.bills), goal: !!c.goal });
  const funds = round2(c.carryover + c.assigned);
  const over = s.tone === 'over';
  const reserved = isReservedLine(c, savingsId);
  const note = rowNote(c, savingsId, prevMonthName);
  const percentUsed = funds > 0.004 ? c.spent / funds * 100 : 0;
  let usedText = 'No funding used';
  if (c.spent < -0.004) usedText = 'Refund received';
  else if (funds > 0.004) usedText = percentUsed > 100 && Math.round(percentUsed) === 100 ? 'Over 100% used' : `${Math.round(percentUsed)}% used`;
  else if (c.spent > 0.004) usedText = 'Spent without funding';
  const leftText = over
    ? `${formatMoney(-c.available)} over plan`
    : reserved ? `${formatMoney(c.available)} reserved` : `${formatMoney(c.available)} left`;
  return (
    <article
      id={`bud-row-${c.category}`}
      className={`bud-category-card${open ? ' is-selected' : ''}${over ? ' is-over' : ''}${s.barTone === 'done' ? ' is-done' : ''}${s.tone === 'near' ? ' is-near' : ''}${flash ? ' is-flash' : ''}`}
      style={{ '--category-hue': hue } as CSSProperties}
      // The whole card opens the panel; the toggle button stays the keyboard and screen-reader control.
      onClick={(e) => { if (!(e.target instanceof Element && e.target.closest('button, a, input, select, textarea, summary'))) onToggle(); }}
    >
      <button
        type="button"
        className="bud-category-card-toggle"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={open ? `bud-panel-${c.category}` : undefined}
        aria-label={`${c.name}: spent ${formatMoney(c.spent)} of ${formatMoney(funds)} in funding, ${leftText}. ${open ? 'Close' : 'Show'} transactions and plan.`}
      >
        <span className="bud-category-card-top">
          <span className="bud-category-icon"><Icon name={categoryIcon(c, savingsId)} /></span>
          <span className="bud-category-name">{c.name}</span>
          <Icon name="chevronRight" className="bud-category-chevron" />
        </span>
        <span className="bud-category-spent-line">
          <strong className="bud-category-spent num">{formatMoney(c.spent)}</strong>
          <span className="bud-category-comparison">spent of <span className="num">{formatMoney(funds)}</span>{Math.abs(c.carryover) > 0.004 ? ' funding' : ''}</span>
        </span>
        <span className="bud-category-bar" aria-hidden="true"><span className={`bud-category-fill is-${s.barTone}`} style={{ width: `${s.pct.toFixed(1)}%` }} /></span>
        <span className="bud-category-foot">
          <span className="bud-category-percent">{usedText}</span>
          <span className={`bud-category-left num bt-${s.tone}`}>{leftText}</span>
        </span>
      </button>
      {reserved && <p className="bud-category-note">Reserved {c.goal ? 'for a goal' : c.debt ? 'for debt payments' : 'for savings'}</p>}
      {note && <p className="bud-category-note">{note}</p>}
      {over && !readOnly && <div className="bud-category-card-actions">
        <button type="button" className="bud-cover-btn" onClick={(e) => actions.onCover(c, e.currentTarget)} aria-label={`Cover it: ${c.name} is over by ${formatMoney(-c.available)}`}>
          Cover {formatMoney(-c.available)}
        </button>
      </div>}
    </article>
  );
}

/** Keep bill funding tools in the selected category's Options instead of lengthening every card. */
function BillsSuggestion({ c, savingsId, actions, readOnly }: { c: EnvelopeLine; savingsId: string | null; actions: RowActions; readOnly: boolean }) {
  if (readOnly || !c.bills || billsShortfall(c, savingsId) <= 0) return null;
  return <div className="bud-bill-suggest">
    <p>Your bills here add up to <strong className="num">{moneyShort(c.bills.total)}</strong> this month.</p>
    <div className="bud-row-btns">
      <button type="button" className="bud-btn bud-btn-sm bud-btn-primary" onClick={() => actions.onPlanBills(c)} aria-label={`Plan ${moneyShort(c.bills.total)} for ${c.name}`}>
        Plan {moneyShort(c.bills.total)}
      </button>
      {c.target?.kind !== 'bills' && <button type="button" className="bud-btn bud-btn-sm" onClick={() => actions.onAlwaysBills(c)} aria-label={`Always plan for the bills in ${c.name}`}>
        Always plan for my bills
      </button>}
    </div>
  </div>;
}
/**
 * Always visible at the bottom of the panel: take the category out of this month's plan, or
 * (only for categories the user made) delete it everywhere. Savings, goal and debt lines have neither.
 */
function RemoveOrDelete({ c, savingsId, monthName, actions }: { c: EnvelopeLine; savingsId: string | null; monthName: string; actions: RowActions }) {
  const uid = useId();
  const [confirm, setConfirm] = useState<'remove' | 'delete' | null>(null);
  const firstBtn = useRef<HTMLButtonElement>(null);
  const refocus = useRef(false);
  // "Keep it" or Escape removes the focused button: put focus back on the choices.
  useEffect(() => { if (confirm === null && refocus.current) { refocus.current = false; firstBtn.current?.focus(); } }, [confirm]);
  const keep = () => { refocus.current = true; setConfirm(null); };
  if (isReservedLine(c, savingsId)) return null;
  const canDelete = !!c.custom;
  return (
    <section className="bud-panel-remove" aria-labelledby={`${uid}-title`}
      onKeyDown={(e) => { if (e.key === 'Escape' && confirm) { e.stopPropagation(); keep(); } }}>
      <h4 id={`${uid}-title`}>Don’t need this category?</h4>
      {confirm === null ? <>
        <div className="bud-row-btns">
          <button ref={firstBtn} type="button" className="bud-btn" onClick={() => setConfirm('remove')}>Remove from {monthName}’s plan</button>
          {canDelete && <button type="button" className="bud-btn" onClick={() => setConfirm('delete')}><Icon name="trash" />Delete category</button>}
        </div>
        {!canDelete && <p className="bud-panel-notice">This category comes with the app, so it can’t be deleted. You can take it out of the plan.</p>}
      </> : <div className="bud-details-confirm" role="group" aria-labelledby={`${uid}-ask`}>
        <p id={`${uid}-ask`}>
          {confirm === 'remove' ? <>
            Remove {c.name} from {monthName}’s plan?{' '}
            {Math.abs(c.available) > 0.004
              ? c.available > 0
                ? `Its ${formatMoney(c.available)} goes back to Not planned yet.`
                : `Its overspending of ${formatMoney(-c.available)} comes out of Not planned yet.`
              : 'Its spending will show under Spending without a plan.'}
          </> : <>
            Delete {c.name} for good? It leaves every month’s plan, its purchases go back to automatic sorting, and rules that use it are deleted. You can undo right after.
          </>}
        </p>
        <div className="bud-row-btns">
          <button type="button" className="bud-btn bud-btn-sm bud-btn-primary" onClick={() => { const run = confirm === 'remove' ? actions.onRemove : actions.onDelete; setConfirm(null); run(c); }}>
            {confirm === 'remove' ? `Remove ${c.name}` : `Delete ${c.name}`}
          </button>
          <button type="button" className="bud-btn bud-btn-sm" onClick={keep} autoFocus>Keep it</button>
        </div>
      </div>}
    </section>
  );
}

/**
 * The row's details (design 3.8, same style as the add-category form): this month's bills with
 * their status in words and a glyph, "+ Add a bill", the leftover note, and the owner's tools
 * (Target, Move money, Rename, See on calendar). A goal's category has "Edit goal →"
 * (the Goals page) instead of Target and Rename. Remove and Delete are in RemoveOrDelete. Esc closes it.
 */
function RowDetails({
  id,
  c,
  savingsId,
  monthName,
  prevMonthName,
  today,
  actions,
  onClose,
  readOnly,
  month,
}: {
  id: string;
  c: EnvelopeLine;
  savingsId: string | null;
  monthName: string;
  prevMonthName: string;
  today: string;
  actions: RowActions;
  onClose: () => void;
  readOnly: boolean;
  month?: string;
}) {
  const uid = useId();
  const [mode, setMode] = useState<'view' | 'rename'>('view');
  const items = c.bills?.items ?? [];
  const counted = items.filter((i) => i.status !== 'skipped');
  const carry = carryNote(c, savingsId, prevMonthName);
  const nextBill = items.find((i) => i.date >= today && i.status !== 'paid' && i.status !== 'skipped');
  const calendarTo = nextBill ? `/recurring?d=${nextBill.date}` : '/recurring';
  // Emergency savings and goals are money kept, not bills: no "Bills in" list or "Add a bill" unless they have some.
  const kept = c.category === savingsId || !!c.goal || !!c.debt;
  const showBills = !kept || items.length > 0;
  const txnParams = new URLSearchParams({ cat: c.category });
  if (month && /^\d{4}-\d{2}$/.test(month)) {
    const year = Number(month.slice(0, 4));
    const mon = Number(month.slice(5, 7));
    txnParams.set('start', `${month}-01`);
    txnParams.set('end', `${month}-${new Date(year, mon, 0).getDate()}`);
  }
  return (
    <div
      id={id}
      className="bud-details"
      role="region"
      aria-label={`${c.name}: bills and options`}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && mode === 'view') {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      {showBills && (
        <h3 className="bud-details-title" id={`${uid}-bills`}>
          Bills in {monthName}
        </h3>
      )}
      {!showBills ? null : items.length ? (
        <ul className="bud-bills" aria-labelledby={`${uid}-bills`}>
          {items.map((i) => {
            const st = billStatusText(i);
            const shown = i.status === 'paid' && i.actual_amount !== null ? i.actual_amount : i.amount;
            return (
              <li key={i.key} className={`bud-bill is-${st.tone}`}>
                <span className="bud-bill-name">{i.name}</span>
                <span className="bud-bill-status">
                  <span className="bud-bill-glyph" aria-hidden="true">
                    {st.glyph}
                  </span>
                  {st.text}
                </span>
                <span className={`bud-bill-amt num${i.status === 'skipped' ? ' is-struck' : ''}`}>
                  {i.status === 'skipped' && <span className="sr-only">Not counted: </span>}
                  {formatMoney(shown)}
                </span>
              </li>
            );
          })}
          {counted.length > 1 && c.bills && (
            <li className="bud-bill is-total">
              <span className="bud-bill-name">Total</span>
              <span className="bud-bill-status" />
              <span className="bud-bill-amt num">{formatMoney(c.bills.total)}</span>
            </li>
          )}
        </ul>
      ) : (
        <p className="bud-details-empty">No bills in {monthName} for this category.</p>
      )}
      {!kept && !readOnly && (
        <button type="button" className="bud-link-btn bud-add-bill" onClick={() => actions.onAddBill(c)}>
          <Icon name="plus" />
          Add a bill
        </button>
      )}
      {carry && !c.goal && <p className="bud-details-note">{carry}</p>}
      {c.debt && (
        <p className="bud-details-note">
          Extra on top of your normal loan payments. {DEBT_NOTE} It’s your debt plan: its amount is set on Reports › Paying off debt.
        </p>
      )}
      {c.goal && <p className="bud-details-note">{goalNote(c)}. It’s a goal: its amount and date are set on the Goals page.</p>}

      {readOnly ? <div className="bud-details-links">
        <Link className="bud-link bud-cal-link" to={calendarTo}>See on calendar <span aria-hidden="true">→</span></Link>
        {c.goal && <Link className="bud-link bud-cal-link" to={`/goals?edit=${c.goal.id}`}>See goal <span aria-hidden="true">→</span></Link>}
        {c.debt && <Link className="bud-link bud-cal-link" to="/reports?tab=debt">See the debt plan <span aria-hidden="true">→</span></Link>}
      </div> : mode === 'rename' ? (
        <RenameForm name={c.name} onCancel={() => setMode('view')} onSave={async (n) => {
          const err = await actions.onRename(c, n);
          if (!err) setMode('view');
          return err;
        }} />
      ) : c.debt ? (
        <div className="bud-details-links">
          <Link className="bud-link bud-cal-link" to="/reports?tab=debt">
            See the plan in Reports <span aria-hidden="true">→</span>
          </Link>
          <button type="button" className="bud-link-btn" onClick={() => actions.onMove(c)}>
            Move money
          </button>
        </div>
      ) : c.goal ? (
        <div className="bud-details-links">
          <Link className="bud-link bud-cal-link" to={`/goals?edit=${c.goal.id}`}>
            Edit goal <span aria-hidden="true">→</span>
          </Link>
          <button type="button" className="bud-link-btn" onClick={() => actions.onMove(c)}>
            Move money
          </button>
        </div>
      ) : (
        <div className="bud-details-links">
          <button type="button" className="bud-link-btn" onClick={() => actions.onTarget(c)}>
            {c.target ? `Target: ${targetLabel(c.target)}` : 'Set a target'}
          </button>
          <button type="button" className="bud-link-btn" onClick={() => actions.onMove(c)}>
            Move money
          </button>
          <button type="button" className="bud-link-btn" onClick={() => setMode('rename')}>
            Rename
          </button>
          <Link className="bud-link bud-cal-link" to={calendarTo}>
            See on calendar <span aria-hidden="true">→</span>
          </Link>
        </div>
      )}
      <Link className="bud-link bud-cal-link" to={`/transactions?${txnParams.toString()}`}>See transactions for {c.name} <span aria-hidden="true">→</span></Link>
    </div>
  );
}

function RenameForm({ name, onCancel, onSave }: { name: string; onCancel: () => void; onSave: (name: string) => Promise<string | null> }) {
  const uid = useId();
  const [value, setValue] = useState(name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);
  const clean = value.trim().replace(/\s+/g, ' ');
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!clean || busy) return;
    if (clean === name) return onCancel();
    setBusy(true);
    const err = await onSave(clean);
    setBusy(false);
    if (err) setError(err);
  }
  return (
    <form
      className="bud-details-rename"
      onSubmit={submit}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onCancel();
        }
      }}
      noValidate
    >
      <label className="bud-field bud-field-grow">
        <span>New name</span>
        <input
          ref={ref}
          className="bud-input"
          value={value}
          maxLength={60}
          autoComplete="off"
          onChange={(e) => {
            setValue(e.target.value);
            setError(null);
          }}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${uid}-err` : undefined}
        />
      </label>
      <button type="submit" className="bud-btn bud-btn-primary" disabled={!clean || busy}>
        {busy ? 'Saving…' : 'Save name'}
      </button>
      <button type="button" className="bud-btn" onClick={onCancel}>
        Cancel
      </button>
      {error && (
        <span id={`${uid}-err`} className="bud-form-hint is-warn" role="alert">
          {error}
        </span>
      )}
    </form>
  );
}

function AddCategoryForm({
  groupName,
  notPlanned,
  onCancel,
  onAdd,
}: {
  groupName: string;
  notPlanned: number;
  onCancel: () => void;
  onAdd: (name: string, plan: number) => Promise<string | null>;
}) {
  const uid = useId();
  const [name, setName] = useState('');
  const [plan, setPlan] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => nameRef.current?.focus(), []);

  const value = plan.trim() === '' ? 0 : parsePlan(plan);
  const bad = value === null;
  const tooMuch = value !== null && value > notPlanned + 0.004;
  const invalid = !name.trim() || bad || tooMuch || busy;
  const hint = error
    ? error
    : bad
      ? 'Type an amount, like 50.'
      : tooMuch
        ? `Only ${formatMoney(notPlanned)} isn’t planned yet. Use a smaller amount, or change the month’s amount.`
        : value
          ? `This will take ${formatMoney(value)} from Not planned yet.`
          : 'You can leave the plan at $0 and set it later.';

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (invalid || value === null) return;
    setBusy(true);
    setError(null);
    const err = await onAdd(name.trim().replace(/\s+/g, ' '), value);
    setBusy(false);
    if (err) setError(err);
  }

  return (
    <form className="bud-add-form" onSubmit={submit} onKeyDown={(e) => e.key === 'Escape' && onCancel()} noValidate>
      <label className="bud-field bud-field-grow">
        <span>Category name</span>
        <input
          ref={nameRef}
          className="bud-input"
          value={name}
          maxLength={60}
          autoComplete="off"
          placeholder={`e.g. ${groupName === 'Bills' ? 'Water bill' : 'Gifts'}`}
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
          }}
        />
      </label>
      <label className="bud-field">
        <span>Plan per month</span>
        <span className="bud-money">
          <span aria-hidden="true">$</span>
          <input
            className="num"
            inputMode="decimal"
            autoComplete="off"
            placeholder="0"
            value={plan}
            onChange={(e) => {
              setPlan(e.target.value);
              setError(null);
            }}
            aria-invalid={bad || tooMuch || undefined}
            aria-describedby={`${uid}-hint`}
          />
        </span>
      </label>
      <button type="submit" className="bud-btn bud-btn-primary" disabled={invalid}>
        {busy ? 'Adding…' : 'Add category'}
      </button>
      <button type="button" className="bud-btn" onClick={onCancel}>
        Cancel
      </button>
      <span id={`${uid}-hint`} className={`bud-form-hint${error || tooMuch || bad ? ' is-warn' : ''}`} role={error ? 'alert' : undefined}>
        {hint}
      </span>
    </form>
  );
}

export function AddGroupForm({ onCancel, onAdd }: { onCancel: () => void; onAdd: (name: string) => Promise<string | null> }) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => ref.current?.focus(), []);
  async function submit(e: FormEvent) {
    e.preventDefault();
    const n = name.trim().replace(/\s+/g, ' ');
    if (!n || busy) return;
    setBusy(true);
    const err = await onAdd(n);
    setBusy(false);
    if (err) setError(err);
  }
  return (
    <form className="bud-add-group-form" onSubmit={submit} onKeyDown={(e) => e.key === 'Escape' && onCancel()} noValidate>
      <label className="bud-field bud-field-grow">
        <span>Group name</span>
        <input
          ref={ref}
          className="bud-input"
          value={name}
          maxLength={60}
          autoComplete="off"
          placeholder="e.g. Pets, Kids, Travel"
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
          }}
        />
      </label>
      <button type="submit" className="bud-btn bud-btn-primary" disabled={!name.trim() || busy}>
        {busy ? 'Adding…' : 'Add group'}
      </button>
      <button type="button" className="bud-btn" onClick={onCancel}>
        Cancel
      </button>
      {error && (
        <span className="bud-form-hint is-warn" role="alert">
          {error}
        </span>
      )}
    </form>
  );
}
