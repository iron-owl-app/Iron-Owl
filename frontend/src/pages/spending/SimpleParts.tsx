import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { BudgetIncomeMode, BudgetMonth, EnvelopeLine, SpendLine } from '../../api';
import { Icon } from '../../components/Icon';
import { formatMoney } from '../../lib/format';
import { monthMoney, monthName, round2, str } from '../../lib/budget';
import { parsePlan } from './GroupCard';

/*
 * Simple view pieces (budget design "Budget Beginner"): plain words, buttons 40px+ (main ones
 * 44–52px), nothing under 13px, amber only with words.
 */

const money = (v: number) => formatMoney(v);
const dollars = (v: number) => (Math.abs(v - Math.round(v)) < 0.005 ? formatMoney(v, { cents: false }) : formatMoney(v));
const shortDate = (iso: string) => new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(new Date(`${iso}T12:00:00`));

// ---------------------------------------------------------------- how this page works

export function HelpCard({ onHide }: { onHide: () => void }) {
  const headId = useId();
  const points: [string, string, string, string][] = [
    ['1', 'Start with the month’s amount', 'Iron Owl suggests how much will come in this month. You can change it any time.', 'ok'],
    ['2', 'Give it a plan', 'Decide how much goes to each category this month. Click any planned amount to change it. Your plans repeat each month. Change one any time; moving money between categories only counts for this month.', 'accent'],
    ['3', 'Spend and watch “Left”', 'As you spend, Left goes down. Amber means you’re close or over; that’s a nudge, not a problem.', 'warn'],
  ];
  return (
    <section className="bud-help" aria-labelledby={headId}>
      <div className="bud-help-head">
        <h2 id={headId}>How this page works</h2>
        <button type="button" className="bud-btn bud-btn-sm" onClick={onHide}>
          Got it, hide this
        </button>
      </div>
      <ol className="bud-help-points">
        {points.map(([n, title, body, tone]) => (
          <li key={n}>
            <span className={`bud-help-n is-${tone}`} aria-hidden="true">
              {n}
            </span>
            <span>
              <strong>{title}</strong>
              <span>{body}</span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

// ---------------------------------------------------------------- the three amount tiles

export const MODE_COPY: Record<BudgetIncomeMode, { label: string; body: string }> = {
  expected: {
    label: 'Count paychecks I expect this month',
    body: 'The month starts with the pay you expect, even before it arrives.',
  },
  off: {
    label: 'Only money that’s already in my accounts',
    body: 'The month counts only money that has already arrived.',
  },
};

export function ModeChoice({ value, onChange, name }: { value: BudgetIncomeMode; onChange: (v: BudgetIncomeMode) => void; name: string }) {
  const legendId = useId();
  return (
    <fieldset className="bud-mode" aria-labelledby={legendId}>
      <legend id={legendId} className="bud-mode-legend">
        What counts as this month’s money?
      </legend>
      {(['expected', 'off'] as const).map((m) => (
        <label key={m} className={`bud-mode-opt${value === m ? ' is-on' : ''}`}>
          <input type="radio" name={name} value={m} checked={value === m} onChange={() => onChange(m)} />
          <span>
            <strong>{MODE_COPY[m].label}</strong>
            <span>{MODE_COPY[m].body}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

export function AmountTiles({
  bm,
  onSaveAmount,
}: {
  bm: BudgetMonth;
  /** Resolves to an error message to show, or null once saved. */
  onSaveAmount: (mode: BudgetIncomeMode, amount: number | null) => Promise<string | null>;
}) {
  const t = monthMoney(bm);
  const [editing, setEditing] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const name = monthName(bm.month);
  const inc = bm.income;
  const allPlanned = t.notPlanned < 0.005;

  let received = `Received so far: ${money(inc.received)}`;
  if (inc.mode === 'expected' && inc.next) received += ` · Next paycheck expected ${shortDate(inc.next.date)}`;
  else if (inc.mode === 'expected' && inc.pending > 0.004 && inc.event?.kind !== 'less') received += ` · ${money(inc.pending)} still expected`;
  const moneySub = inc.mode === 'off' ? `${received} · Only money already in your accounts` : received;

  return (
    <section className="bud-tiles" aria-label={`${name} at a glance`} aria-live="polite">
      <div className="bud-tile">
        <span className="bud-tile-label">Money for {name}</span>
        <span className="bud-tile-value num">{money(t.money)}</span>
        <span className="bud-tile-sub">{moneySub}</span>
        {editing ? (
          <AmountEditor
            bm={bm}
            onCancel={() => {
              setEditing(false);
              requestAnimationFrame(() => btnRef.current?.focus());
            }}
            onSave={async (mode, amount) => {
              const err = await onSaveAmount(mode, amount);
              if (!err) {
                setEditing(false);
                requestAnimationFrame(() => btnRef.current?.focus());
              }
              return err;
            }}
          />
        ) : (
          <button ref={btnRef} type="button" className="bud-btn bud-btn-sm bud-tile-btn" onClick={() => setEditing(true)}>
            {inc.mode === 'expected' ? 'Change amount' : 'How it’s counted'}
          </button>
        )}
      </div>
      <div className="bud-tile">
        <span className="bud-tile-label">Planned</span>
        <span className="bud-tile-value num">{money(t.planned)}</span>
        <span className="bud-tile-sub">Spent so far: {money(t.spent)}</span>
      </div>
      <div className="bud-tile">
        <span className="bud-tile-label">Not planned yet</span>
        <span className={`bud-tile-value num${allPlanned ? ' is-ok' : ' is-unplanned'}`}>{money(t.notPlanned)}</span>
        <span className={`bud-tile-sub${allPlanned && !t.overPlanned ? ' is-ok' : ''}`}>
          {t.overPlanned ? `Plans are ${money(t.overPlanned)} over` : allPlanned ? '✓ Every dollar has a plan' : 'Give it a plan or leave it for next month'}
        </span>
      </div>
    </section>
  );
}

export function AmountEditor({
  bm,
  onCancel,
  onSave,
}: {
  bm: BudgetMonth;
  onCancel: () => void;
  onSave: (mode: BudgetIncomeMode, amount: number | null) => Promise<string | null>;
}) {
  const uid = useId();
  const [mode, setMode] = useState<BudgetIncomeMode>(bm.income.mode);
  // With the switch off, the amount the month would have with expected paychecks counted.
  const inc = bm.income;
  const whenOn = round2(bm.month_money + (inc.mode === 'off' ? Math.max(0, (inc.expected ?? inc.received) - inc.received) : 0));
  const [draft, setDraft] = useState(str(inc.mode === 'off' ? whenOn : bm.month_money));
  const [touched, setTouched] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const first = useRef<HTMLDivElement>(null);
  const canChange = bm.is_current;

  useEffect(() => {
    if (mode === 'expected') inputRef.current?.focus();
    else first.current?.querySelector('input')?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    let amount: number | null = null;
    if (mode === 'expected' && touched) {
      amount = parsePlan(draft);
      if (amount === null) return setErr('Type an amount, like 4800.');
      if (amount + 0.004 < bm.assigned) {
        return setErr(`Your plans add up to ${money(bm.assigned)}. Lower some plans first, or pick at least ${money(bm.assigned)}.`);
      }
    }
    setBusy(true);
    setErr(null);
    const res = await onSave(mode, amount);
    setBusy(false);
    if (res) setErr(res);
  }

  return (
    <form className="bud-amount-form" onSubmit={submit} onKeyDown={(e) => e.key === 'Escape' && (e.stopPropagation(), onCancel())} noValidate>
      <div ref={first}>
        <ModeChoice value={mode} onChange={(m) => (setMode(m), setErr(null))} name={`${uid}-mode`} />
      </div>
      {mode === 'expected' && canChange && (
        <label className="bud-field">
          <span>Money for {monthName(bm.month)}</span>
          <span className="bud-money is-strong">
            <span aria-hidden="true">$</span>
            <input
              ref={inputRef}
              className="num"
              inputMode="decimal"
              autoComplete="off"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setTouched(true);
                setErr(null);
              }}
              aria-invalid={!!err || undefined}
              aria-describedby={err ? `${uid}-err` : undefined}
            />
          </span>
        </label>
      )}
      <div className="bud-row-btns">
        <button type="submit" className="bud-btn bud-btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className="bud-btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
      {err && (
        <p id={`${uid}-err`} className="bud-form-hint is-warn" role="alert">
          {err}
        </p>
      )}
    </form>
  );
}

// ---------------------------------------------------------------- notes

export interface NoteAction {
  label: string;
  run: (el: HTMLElement) => void;
  primary?: boolean;
}

export function Note({
  tone,
  icon,
  title,
  body,
  actions,
  role = 'status',
}: {
  tone: 'ok' | 'warn' | 'accent';
  icon: string;
  title: ReactNode;
  body: ReactNode;
  actions?: NoteAction[];
  role?: 'status' | 'alert';
}) {
  return (
    <div className={`bud-note is-${tone}`} role={role}>
      <span className="bud-note-ic" aria-hidden="true">
        {icon}
      </span>
      <div className="bud-note-text">
        <div className="bud-note-title">{title}</div>
        <div className="bud-note-body">{body}</div>
      </div>
      {actions && actions.length > 0 && (
        <div className="bud-note-actions">
          {actions.map((a) => (
            <button key={a.label} type="button" className={`bud-btn${a.primary ? ' bud-btn-strong' : ''}`} onClick={(e) => a.run(e.currentTarget)}>
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function IncomeNote({
  bm,
  savingsName,
  savingsFree,
  onMore,
  onLess,
}: {
  bm: BudgetMonth;
  savingsName: string | null;
  /** Money left in Emergency savings that "Use savings" can take. */
  savingsFree: number;
  onMore: (choice: 'add' | 'save' | 'leave') => void;
  onLess: (choice: 'unplanned' | 'categories' | 'savings' | 'next', el: HTMLElement) => void;
}) {
  const ev = bm.income.event;
  if (!ev) return null;
  const name = monthName(bm.month);
  if (ev.kind === 'more') {
    const when = ev.date ? `It came in on ${shortDate(ev.date)}${ev.name ? ` (${ev.name})` : ''}. ` : '';
    const actions: NoteAction[] = [{ label: 'Add it to this month’s plan', run: () => onMore('add'), primary: true }];
    if (savingsName) actions.push({ label: `Put it in ${savingsName}`, run: () => onMore('save') });
    actions.push({ label: 'Leave it for now', run: () => onMore('leave') });
    return (
      <Note
        tone="ok"
        icon="+"
        title={`You got ${dollars(ev.amount)} more than expected this month.`}
        body={`${when}It’s already counted in Not planned yet. Choose what to do with it.`}
        actions={actions}
      />
    );
  }
  const actions: NoteAction[] = [];
  if (ev.can_use_unplanned) actions.push({ label: 'Use money that isn’t planned yet', run: (el) => onLess('unplanned', el), primary: true });
  actions.push({ label: 'Take it from categories with money left', run: (el) => onLess('categories', el), primary: !ev.can_use_unplanned });
  if (savingsName && savingsFree > 0.004) actions.push({ label: `Use ${savingsName}`, run: (el) => onLess('savings', el) });
  actions.push({ label: 'Leave it for next month', run: (el) => onLess('next', el) });
  // Phase 2: the paycheck that didn't come (calendar occurrences), when the server knows it.
  const missed = ev.date ? `Your ${shortDate(ev.date)} paycheck${ev.name ? ` from ${ev.name}` : ''} hasn’t come in` : null;
  return (
    <Note
      tone="warn"
      icon="−"
      title={
        ev.reason === 'missing'
          ? missed
            ? `${missed}.`
            : `The paycheck ${name}’s plan counted on hasn’t come in.`
          : missed
            ? `${missed}, so this month’s pay is ${dollars(ev.amount)} under the plan.`
            : `This month’s pay came in ${dollars(ev.amount)} under what the plan counted on.`
      }
      body={`${name}’s plan counted on ${dollars(ev.expected)}, but ${dollars(ev.received)} came in. Choose how to make up the difference.`}
      actions={actions}
    />
  );
}

export function CoverBanner({ over, onCover }: { over: EnvelopeLine[]; onCover: (c: EnvelopeLine, el: HTMLElement) => void }) {
  if (!over.length) return null;
  const first = over[0]!;
  const title = over.length === 1 ? `${first.name} is ${money(-first.available)} over` : `${over.length} categories are over: ${over.map((c) => c.name).join(', ')}`;
  return (
    <Note
      tone="warn"
      icon="!"
      title={title}
      body="Cover it by taking money from another category that has some left."
      actions={[{ label: over.length === 1 ? 'Cover it' : `Cover ${first.name}`, run: (el) => onCover(first, el), primary: true }]}
    />
  );
}

export function StatusMessage({
  bm,
  savingsName,
  onSave,
  onLower,
}: {
  bm: BudgetMonth;
  savingsName: string | null;
  onSave: () => void;
  onLower: (el: HTMLElement) => void;
}) {
  const t = monthMoney(bm);
  if (t.overPlanned > 0.004) {
    return (
      <Note
        tone="warn"
        icon="!"
        title={`You’ve planned ${money(t.overPlanned)} more than you have.`}
        body="Lower a plan to fix it."
        actions={[{ label: 'Lower plans', run: onLower, primary: true }]}
      />
    );
  }
  if (t.notPlanned < 0.005) {
    return <Note tone="ok" icon="✓" title="Every dollar has a plan" body="Nice work. If more money comes in this month, it will show up under Not planned yet." />;
  }
  return (
    <Note
      tone="accent"
      icon="$"
      title={`${dollars(t.notPlanned)} doesn’t have a plan yet`}
      body="That’s fine. You can add it to a category, put it in savings, or leave it for next month."
      actions={savingsName ? [{ label: `Put it in ${savingsName}`, run: onSave, primary: true }] : undefined}
    />
  );
}

// ---------------------------------------------------------------- spending without a plan

export function NotInPlanCard({ bm, onAdd, readOnly = false }: { bm: BudgetMonth; onAdd: (u: SpendLine) => void; readOnly?: boolean }) {
  const headId = useId();
  // Phase 2: bill categories that aren't in the plan show here too ("Your bills here add up to $X").
  const bills = new Map(bm.bills_outside.map((b) => [b.category, b.total]));
  const rows = [
    ...bm.unbudgeted.map((u) => ({ ...u, bills: bills.get(u.category) ?? null })),
    ...bm.bills_outside
      .filter((b) => !bm.unbudgeted.some((u) => u.category === b.category))
      .map((b) => ({ category: b.category, name: b.name, hue: b.hue, spent: 0, bills: b.total as number | null })),
  ];
  if (!rows.length) return null;
  const total = round2(bm.unbudgeted.reduce((a, u) => a + u.spent, 0));
  const start = `${bm.month}-01`;
  const end = `${bm.month}-${String(bm.days_in_month).padStart(2, '0')}`;
  return (
    <section className="bud-card bud-unplanned" aria-labelledby={headId}>
      <div className="bud-card-head">
        <h2 id={headId}>Spending without a plan</h2>
        <span className="num bud-card-total">{money(total)}</span>
      </div>
      <p className="bud-card-intro">
        {bm.unbudgeted.length
          ? 'This was spent this month in categories that aren’t in your plan. It still comes out of your money, so it lowers Not planned yet.'
          : 'These categories have bills this month but aren’t in your plan yet.'}
      </p>
      <ul className="bud-unplanned-list">
        {rows.map((u) => {
          // Planning it covers what was spent, or the month's bills when they're more.
          const plan = round2(Math.max(u.spent, u.bills ?? 0));
          return (
            <li key={u.category}>
              <span className="bud-unplanned-name">
                <span>{u.name}</span>
                {u.bills !== null && <span className="bud-unplanned-bills">Your bills here add up to {dollars(u.bills)} this month.</span>}
                {u.spent > 0.004 && (
                  <Link to={`/transactions?cat=${encodeURIComponent(u.category)}&start=${start}&end=${end}`} className="bud-link">
                    See what was bought
                  </Link>
                )}
              </span>
              <span className="num bud-unplanned-amt">{u.spent > 0.004 ? money(u.spent) : <span className="bud-unplanned-none">Nothing spent yet</span>}</span>
              {!readOnly && <button
                type="button"
                className="bud-btn bud-btn-sm"
                onClick={() => onAdd({ category: u.category, name: u.name, hue: u.hue, spent: plan })}
                aria-label={`Add ${u.name} to my plan with ${money(plan)}`}
              >
                <Icon name="plus" />
                Add to my plan ({dollars(plan)})
              </button>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------- more options

export interface MoreTile {
  key: string;
  title: string;
  body: string;
  pressed?: boolean;
  run: () => void;
}

export function MoreOptions({ tiles }: { tiles: MoreTile[] }) {
  const uid = useId();
  const [open, setOpen] = useState(false);
  return (
    <section className="bud-more">
      <button type="button" className="bud-more-head" aria-expanded={open} aria-controls={`${uid}-tiles`} onClick={() => setOpen(!open)}>
        <span>
          <span className="bud-more-title">More options</span>
          <span className="bud-more-sub">Move money, targets, carried-over amounts and quick fill. You don’t need these to use your budget.</span>
        </span>
        <Icon name="chevronDown" className={`bud-more-chev${open ? ' is-open' : ''}`} />
      </button>
      <div id={`${uid}-tiles`} className="bud-more-tiles" hidden={!open}>
        {tiles.map((t) => (
          <button key={t.key} type="button" className="bud-more-tile" onClick={t.run} aria-pressed={t.pressed}>
            <span className="bud-more-tile-title">
              {t.title}
              {t.pressed !== undefined && <span className={`bud-onoff${t.pressed ? ' is-on' : ''}`}>{t.pressed ? 'On' : 'Off'}</span>}
            </span>
            <span className="bud-more-tile-body">{t.body}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
