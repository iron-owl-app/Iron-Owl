import { useEffect, useId, useState, type CSSProperties, type FormEvent } from 'react';
import { api, errorMessage, type BudgetIncomeMode, type BudgetMonth, type EnvelopeLine } from '../../api';
import { Modal } from '../../components/Modal';
import { formatMoney } from '../../lib/format';
import { addMonths, monthName, round2, targetLabel } from '../../lib/budget';
import { parsePlan } from './GroupCard';
import { ModeChoice } from './SimpleParts';

const money = (v: number) => formatMoney(v);

// ---------------------------------------------------------------- Cover it / lower plans

export interface PickItem {
  /** null = Not planned yet. */
  id: string | null;
  name: string;
  avail: number;
  availText: string;
}

export interface PickGroup {
  key: string;
  name: string;
  /** null = the accent (Money without a plan). */
  hue: number | null;
  items: PickItem[];
}

/**
 * The source picker (design "Cover it"): a centered 400px dialog listing where money can come
 * from, Not planned yet first, then each group's categories with money left.
 */
export function CoverPicker({
  open,
  title,
  sub,
  need,
  groups,
  noneText,
  busy,
  onTake,
  onClose,
}: {
  open: boolean;
  title: string;
  sub: string;
  need: number;
  groups: PickGroup[];
  noneText: string;
  busy: boolean;
  onTake: (item: PickItem, amount: number) => void;
  onClose: () => void;
}) {
  return (
    <Modal open={open} width={400} title={title} subtitle={sub} onClose={onClose} busy={busy}>
      <div className="bud-pick">
        {groups.length === 0 ? (
          <p className="bud-pick-none">{noneText}</p>
        ) : (
          groups.map((g) => (
            <div key={g.key} className="bud-pick-group" style={g.hue === null ? undefined : ({ '--h': g.hue } as CSSProperties)}>
              <div className={`bud-pick-head${g.hue === null ? ' is-accent' : ''}`}>
                <span aria-hidden="true" />
                {g.name}
              </div>
              {g.items.map((s) => {
                const amt = round2(Math.min(need, s.avail));
                const all = amt + 0.004 < need;
                return (
                  <button key={s.id ?? 'rta'} type="button" className="bud-pick-item" disabled={busy || amt < 0.005} onClick={() => onTake(s, amt)}>
                    <span className="bud-pick-name">
                      <span>{s.name}</span>
                      <span>{s.availText}</span>
                    </span>
                    <span className="bud-pick-take num">{all ? `Take ${money(amt)} (all of it)` : `Take ${money(amt)}`}</span>
                  </button>
                );
              })}
            </div>
          ))
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- move money

export function MoveMoneyModal({
  open,
  sections,
  savingsId = null,
  initialFrom = null,
  onClose,
  onMove,
}: {
  open: boolean;
  /** Opened from a category's details: that category is the "from" already. */
  initialFrom?: string | null;
  /** The page's groups in order (each with its categories), so both lists read like the page. */
  sections: { key: string; name: string; lines: EnvelopeLine[] }[];
  /** The designated savings category, identified by server metadata. */
  savingsId?: string | null;
  onClose: () => void;
  /** to = category id or 'rta' (Not planned yet). Resolves to an error message, or null. */
  onMove: (from: EnvelopeLine, to: string, amount: number) => Promise<string | null>;
}) {
  const uid = useId();
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('rta');
  const [amount, setAmount] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setFrom(initialFrom ?? '');
    setTo('rta');
    setAmount('');
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const lines = sections.flatMap((s) => s.lines);
  const reservedPurpose = (c: EnvelopeLine): string | null => c.goal
    ? c.goal.kind === 'emergency' ? 'emergency savings' : 'a savings goal'
    : c.debt ? 'debt payments' : c.category === savingsId ? 'savings' : null;
  const src = lines.find((c) => c.category === from) ?? null;
  const movable = src ? round2(src.carryover + src.assigned) : 0;
  const value = parsePlan(amount);
  const tooMuch = value !== null && src !== null && value > movable + 0.004;
  const ok = !!src && value !== null && value > 0.004 && !tooMuch && to !== from && !busy;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ok || !src || value === null) return;
    setBusy(true);
    const err = await onMove(src, to, value);
    setBusy(false);
    if (err) setError(err);
  }

  return (
    <Modal
      open={open}
      width={460}
      title="Move money between categories"
      subtitle="Take from one plan and add to another. The month’s total doesn’t change."
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form={`${uid}-f`} className="btn btn-primary" disabled={!ok}>
            {busy ? 'Moving…' : 'Move'}
          </button>
        </>
      }
    >
      <form id={`${uid}-f`} className="bud-modal-form" onSubmit={submit} noValidate>
        <label className="bud-field">
          <span>From</span>
          <select className="bud-input" value={from} onChange={(e) => (setFrom(e.target.value), setError(null))}>
            <option value="">Pick a category…</option>
            {sections.map((s) => {
              const have = s.lines.filter((c) => c.carryover + c.assigned > 0.004);
              return have.length ? (
                <optgroup key={s.key} label={s.name}>
                  {have.map((c) => (
                    <option key={c.category} value={c.category}>
                      {c.name} ({money(c.available)} left){reservedPurpose(c) ? ` · Reserved for ${reservedPurpose(c)}` : ''}
                    </option>
                  ))}
                </optgroup>
              ) : null;
            })}
          </select>
        </label>
        <label className="bud-field">
          <span>To</span>
          <select className="bud-input" value={to} onChange={(e) => (setTo(e.target.value), setError(null))}>
            <option value="rta">Not planned yet</option>
            {sections.map((s) => {
              const into = s.lines.filter((c) => c.category !== from);
              return into.length ? (
                <optgroup key={s.key} label={s.name}>
                  {into.map((c) => (
                    <option key={c.category} value={c.category}>
                      {c.name}{reservedPurpose(c) ? ` · Reserved for ${reservedPurpose(c)}` : ''}
                    </option>
                  ))}
                </optgroup>
              ) : null;
            })}
          </select>
        </label>
        <label className="bud-field">
          <span>Amount</span>
          <span className="bud-money">
            <span aria-hidden="true">$</span>
            <input className="num" inputMode="decimal" autoComplete="off" value={amount} onChange={(e) => (setAmount(e.target.value), setError(null))} aria-invalid={tooMuch || undefined} />
          </span>
        </label>
        {src && reservedPurpose(src) && <p className="bud-form-hint">This takes money reserved for {reservedPurpose(src)}. Choose this source only if you want to use that money for another plan.</p>}
        <p className={`bud-form-hint${error || tooMuch ? ' is-warn' : ''}`} role={error ? 'alert' : undefined}>
          {error ?? (tooMuch && src ? `You can move at most ${money(movable)} out of ${src.name}.` : src && value ? `${money(value)} from ${src.name}.` : 'Pick where the money comes from.')}
        </p>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- targets

export function TargetsModal({
  open,
  bm,
  lines,
  onClose,
  onEdit,
  onFund,
}: {
  open: boolean;
  bm: BudgetMonth;
  lines: EnvelopeLine[];
  onClose: () => void;
  onEdit: (c: EnvelopeLine) => void;
  onFund: () => void;
}) {
  return (
    <Modal
      open={open}
      width={520}
      title="Targets"
      subtitle="Save toward a set amount each month, or by a date. Iron Owl shows what each one still needs."
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Close
          </button>
          <button type="button" className="btn btn-primary" onClick={onFund} disabled={bm.needed_total < 0.005 || bm.ready_to_assign < 0.005}>
            Fund targets ({money(bm.needed_total)} needed)
          </button>
        </>
      }
    >
      <ul className="bud-target-list">
        {lines.map((c) => (
          <li key={c.category}>
            <span className="bud-target-name">
              <span>{c.name}</span>
              <span>{c.target ? `${targetLabel(c.target)}${c.target.needed > 0.004 ? ` · ${money(c.target.needed)} still needed` : ' · on track'}` : 'No target'}</span>
            </span>
            <button type="button" className="bud-btn bud-btn-sm" onClick={() => onEdit(c)}>
              {c.target ? 'Change' : 'Set a target'}
            </button>
          </li>
        ))}
      </ul>
    </Modal>
  );
}

// ---------------------------------------------------------------- quick fill

type Fill = 'plans' | 'spending' | 'targets';

export function QuickFillModal({
  open,
  bm,
  onClose,
  onFill,
  onFund,
}: {
  open: boolean;
  bm: BudgetMonth;
  onClose: () => void;
  onFill: (assigned: Record<string, number>, title: string) => void;
  onFund: () => void;
}) {
  const [prev, setPrev] = useState<BudgetMonth | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const prevMonth = addMonths(bm.month, -1);
  const hasPrev = !!bm.earliest_month && prevMonth >= bm.earliest_month;
  useEffect(() => {
    if (!open || !hasPrev) return;
    let live = true;
    setPrev(null);
    api.budgets
      .get(prevMonth)
      .then((p) => live && setPrev(p))
      .catch((e: unknown) => live && setErr(errorMessage(e)));
    return () => {
      live = false;
    };
  }, [open, hasPrev, prevMonth]);

  const free = Math.max(0, bm.ready_to_assign);
  const lines = bm.categories;
  // Setup's "money you already had" row in last month isn't a plan to copy.
  const prevPlans = prev ? prev.categories.filter((c) => !c.seeded && c.category !== prev.seed_category) : [];
  const build = (f: Exclude<Fill, 'targets'>): Record<string, number> | null => {
    if (!prev || (f === 'plans' && !prevPlans.length)) return null;
    const by = new Map((f === 'plans' ? prevPlans : prev.categories).map((c) => [c.category, f === 'plans' ? c.assigned : c.spent]));
    const spentPrev = new Map(prev.unbudgeted.map((u) => [u.category, u.spent]));
    // A goal's category follows the goal's rule instead (the server's plan for it this month):
    // a reached goal gets nothing more, and an active one keeps its monthly amount.
    return Object.fromEntries(
      lines.map((c) => [c.category, c.goal ? c.goal.plan : c.debt ? c.debt.extra : round2(Math.max(-c.carryover, by.get(c.category) ?? (f === 'spending' ? spentPrev.get(c.category) ?? 0 : 0)))]),
    );
  };
  const name = monthName(prevMonth);
  const options: { key: Fill; title: string; body: string; total: number | null; extra: number }[] = [];
  for (const f of ['plans', 'spending'] as const) {
    const a = build(f);
    const total = a ? round2(Object.values(a).reduce((s, v) => s + v, 0)) : null;
    options.push({
      key: f,
      title: f === 'plans' ? `Use ${name}’s plans` : `Use what you spent in ${name}`,
      body: f === 'plans' ? `Every category gets what you planned in ${name}.` : `Every category gets what it spent in ${name}.`,
      total,
      extra: total === null ? 0 : round2(total - bm.assigned),
    });
  }
  options.push({ key: 'targets', title: 'Fill targets', body: 'Give each target what it still needs this month.', total: bm.needed_total, extra: bm.needed_total });

  return (
    <Modal open={open} width={480} title="Quick fill" subtitle="Fill in this month’s plans in one step. Undo puts them back." onClose={onClose}>
      <div className="bud-fill">
        {!hasPrev && <p className="bud-form-hint">There’s no {name} budget to copy from.</p>}
        {err && <p className="bud-form-hint is-warn">{err}</p>}
        {options.map((o) => {
          const short = o.key === 'targets' ? (o.total ?? 0) < 0.005 || free < 0.005 : o.extra > free + 0.004;
          const unavailable = o.total === null;
          return (
            <button
              key={o.key}
              type="button"
              className="bud-fill-item"
              disabled={unavailable || short}
              onClick={() => {
                if (o.key === 'targets') return onFund();
                const a = build(o.key);
                if (a) onFill(a, o.title);
              }}
            >
              <span>
                <strong>{o.title}</strong>
                <span>
                  {unavailable
                    ? !hasPrev
                      ? 'Nothing to copy'
                      : prev
                        ? `Nothing planned in ${name}`
                        : 'Loading…'
                    : o.key === 'targets'
                      ? (o.total ?? 0) < 0.005
                        ? 'Every target has what it needs.'
                        : o.body
                      : short
                        ? `Needs ${money(round2(o.extra - free))} more than isn’t planned yet.`
                        : o.body}
                </span>
              </span>
              <span className="num bud-fill-amt">{o.total === null ? '' : money(o.total)}</span>
            </button>
          );
        })}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- expected income and savings

export function IncomeSettingsModal({
  open,
  bm,
  onClose,
  onSave,
}: {
  open: boolean;
  bm: BudgetMonth;
  onClose: () => void;
  /** Resolves to an error message, or null once saved. */
  onSave: (mode: BudgetIncomeMode, savings: string | null) => Promise<string | null>;
}) {
  const uid = useId();
  const [mode, setMode] = useState<BudgetIncomeMode>(bm.income.mode);
  const [savings, setSavings] = useState(bm.savings_category ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setMode(bm.income.mode);
    setSavings(bm.savings_category ?? '');
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const options = [...bm.categories.map((c) => ({ id: c.category, name: c.name })), ...bm.addable.map((a) => ({ id: a.id, name: a.name }))];

  async function save() {
    setBusy(true);
    const err = await onSave(mode, savings || null);
    setBusy(false);
    setError(err);
  }

  return (
    <Modal
      open={open}
      width={520}
      title="Expected income and savings"
      subtitle={bm.income.suggested !== null ? `Lately about ${money(bm.income.suggested)} has come in each month.` : undefined}
      onClose={onClose}
      busy={busy}
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
      <div className="bud-modal-form">
        <ModeChoice value={mode} onChange={setMode} name={`${uid}-mode`} />
        <label className="bud-field">
          <span>Savings category</span>
          <select className="bud-input" value={savings} onChange={(e) => setSavings(e.target.value)}>
            <option value="">None</option>
            {options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
          <span className="bud-field-help">Money you set aside, not money to spend. Home leaves it out of “Left to spend”.</span>
        </label>
        {error && (
          <p className="bud-form-hint is-warn" role="alert">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
