import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type Cadence, type PaycheckItem, type RecurringPatch } from '../../api';
import { useApi } from '../../lib/useApi';
import { toISODate } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { DialogActions, StDialog, StOptions, StSwitch, dollars, monthDay, useFail, useSay } from './parts';

/** How often (the design's four) and the per-month factor Budget uses. */
const FREQS: { value: Cadence; label: string; perMonth: number }[] = [
  { value: 'weekly', label: 'Every week', perMonth: 52 / 12 },
  { value: 'biweekly', label: 'Every other week', perMonth: 26 / 12 },
  { value: 'semimonthly', label: 'Twice a month', perMonth: 2 },
  { value: 'monthly', label: 'Once a month', perMonth: 1 },
];
/** Items made elsewhere (Bills and paychecks) can have other schedules. */
const OTHER: Partial<Record<Cadence, { label: string; perMonth: number }>> = {
  quarterly: { label: 'Every 3 months', perMonth: 1 / 3 },
  yearly: { label: 'Once a year', perMonth: 1 / 12 },
  once: { label: 'Just once', perMonth: 0 },
};
const freqOf = (c: Cadence) => FREQS.find((f) => f.value === c) ?? OTHER[c] ?? FREQS[3]!;

type PayDraft = { mode: 'new' | 'edit'; id?: number; name: string; amountText: string; cadence: Cadence; next: string };

const parseAmount = (t: string) => {
  const n = Number(t.replace(/[$,\s]/g, ''));
  return Number.isFinite(n) && n > 0 ? Math.round(n * 100) / 100 : 0;
};

/**
 * Settings › Paychecks (design D7 tab 3): the money that comes in each month, which Budget
 * suggests as the month's amount. Items are recurring items with money in (GET /api/paychecks);
 * adding, editing and deleting go through /api/recurring (delete is undone from a snapshot).
 */
export function PaychecksTab() {
  const say = useSay();
  const fail = useFail();
  const pay = useApi(() => api.paychecks(), []);
  const [pd, setPd] = useState<PayDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [notifyBusy, setNotifyBusy] = useState(false);
  const uid = useId();
  const p = pay.data;

  const openNew = () => setPd({ mode: 'new', name: '', amountText: '', cadence: 'monthly', next: toISODate(new Date()) });
  const openEdit = (it: PaycheckItem) =>
    setPd({ mode: 'edit', id: it.id, name: it.name, amountText: String(it.amount), cadence: it.cadence, next: it.next_date ?? toISODate(new Date()) });

  const amount = pd ? parseAmount(pd.amountText) : 0;
  const name = pd?.name.trim() ?? '';
  const ok = !!pd && !!name && amount > 0 && /^\d{4}-\d{2}-\d{2}$/.test(pd.next);

  async function save() {
    if (!pd || !ok) return;
    setBusy(true);
    try {
      if (pd.mode === 'new') {
        await api.recurring.create({ name, amount, cadence: pd.cadence, next_date: pd.next });
      } else if (pd.id !== undefined) {
        const cur = p?.items.find((x) => x.id === pd.id);
        const patch: RecurringPatch = {};
        if (!cur || cur.name !== name) patch.name = name;
        if (!cur || cur.amount !== amount) patch.amount = amount;
        if (!cur || cur.cadence !== pd.cadence) patch.cadence = pd.cadence;
        if (!cur || cur.next_date !== pd.next) patch.next_date = pd.next;
        if (Object.keys(patch).length) await api.recurring.update(pd.id, patch);
      }
      setPd(null);
      pay.reload();
      say('Saved. Budget will suggest the new monthly amount next month.');
    } catch (e) {
      fail('Couldn’t save it', e);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (pd?.id === undefined) return;
    const id = pd.id;
    const label = name || 'It';
    setBusy(true);
    try {
      const snap = await api.recurring.snapshot(id);
      await api.recurring.remove(id);
      setPd(null);
      pay.reload();
      say(`${label} was removed.`, {
        undo: async () => {
          await api.recurring.restore(snap);
          pay.reload();
        },
      });
    } catch (e) {
      fail('Couldn’t remove it', e);
    } finally {
      setBusy(false);
    }
  }

  async function setNotify(on: boolean) {
    if (!p) return;
    setNotifyBusy(true);
    pay.setData({ ...p, notify: on });
    try {
      const s = await api.settings.update({ paycheck_notify: on });
      pay.setData((prev) => (prev ? { ...prev, notify: s.paycheck_notify } : prev!));
    } catch (e) {
      pay.setData((prev) => (prev ? { ...prev, notify: !on } : prev!));
      fail('Couldn’t change that', e);
    } finally {
      setNotifyBusy(false);
    }
  }

  const freqOptions = pd && !FREQS.some((f) => f.value === pd.cadence) ? [...FREQS, { value: pd.cadence, label: freqOf(pd.cadence).label }] : FREQS;

  return (
    <section className="st-card" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head is-center">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Money coming in each month</h2>
          <p className="st-desc">Iron Owl adds up your paychecks to suggest the month’s amount on the Budget page. You can still change it there.</p>
        </div>
        {p && (
          <div className="st-total">
            <div className="st-total-num">{dollars(Math.round(p.monthly_total))}</div>
            <div className="st-total-sub">about, each month</div>
          </div>
        )}
      </div>

      {pay.loading && !p ? (
        <div className="st-loading" aria-busy="true" aria-label="Loading paychecks">
          <Skeleton height={56} style={{ borderRadius: 10 }} />
        </div>
      ) : pay.error && !p ? (
        <p className="st-error">
          Iron Owl couldn’t load your paychecks.{' '}
          <button type="button" className="st-link st-link-inline" onClick={pay.reload}>
            Try again
          </button>
        </p>
      ) : (
        <>
          {p?.income_mode === 'off' && (
            <p className="st-empty">
              Right now Budget only counts money that’s already in your accounts, so it won’t suggest this amount. You can change that on the Budget page.
            </p>
          )}
          {p && p.items.length === 0 && <p className="st-empty">Nothing yet. Add your paycheck, pension or Social Security so Budget knows what’s coming.</p>}
          {p?.items.map((it) => (
            <div className="st-item" key={it.id}>
              <span className="st-tile-icon" aria-hidden="true">
                $
              </span>
              <span className="st-item-main">
                <span className="st-item-name">{it.name}</span>
                <span className="st-item-sub">
                  {freqOf(it.cadence).label}
                  {it.next_date ? ` · next one ${monthDay(it.next_date)}` : ''}
                </span>
              </span>
              <span className="st-amt">{dollars(it.amount)}</span>
              <button type="button" className="st-btn st-btn-plain st-btn-sm" onClick={() => openEdit(it)} aria-label={`Edit ${it.name}`}>
                Edit
              </button>
            </div>
          ))}
          {p && (
            <p className="st-help">
              Money moved between your own accounts isn’t counted here. If a paycheck is missing, open it in{' '}
              <Link to="/recurring" className="st-link st-link-inline">
                Bills and paychecks
              </Link>{' '}
              and set its Budget category to Income.
            </p>
          )}
          <div className="st-item">
            <button type="button" className="st-btn st-btn-outline" onClick={openNew}>
              + Add money that comes in
            </button>
            <span className="st-help-faint">For example, a pension or Social Security.</span>
          </div>
          {p && (
            <div className="st-row">
              <span className="st-row-text">
                <span className="st-label">Tell me when a paycheck is different</span>
                <span className="st-help" id={`${uid}-notify`}>
                  If one comes in higher or lower than expected, Budget shows a short note with a few choices.
                </span>
              </span>
              <StSwitch
                checked={p.notify}
                onChange={(on) => void setNotify(on)}
                label="Tell me when a paycheck is different"
                describedBy={`${uid}-notify`}
                disabled={notifyBusy}
              />
            </div>
          )}
        </>
      )}
      <div className="st-link-row">
        <Link to="/spending" className="st-link">
          Go to Budget →
        </Link>
      </div>

      <StDialog
        open={pd !== null}
        title={pd?.mode === 'edit' ? `Edit ${pd.name.trim() || 'money coming in'}` : 'Add money that comes in'}
        onClose={() => setPd(null)}
        onSubmit={() => void save()}
        busy={busy}
      >
        {pd && (
          <>
            <div className="st-field">
              <label className="st-field-label" htmlFor={`${uid}-name`}>
                What it’s called
              </label>
              <input
                id={`${uid}-name`}
                className="st-input"
                value={pd.name}
                maxLength={100}
                autoComplete="off"
                placeholder="For example, Paycheck or Pension"
                onChange={(e) => setPd({ ...pd, name: e.target.value })}
              />
            </div>
            <div className="st-field">
              <label className="st-field-label" htmlFor={`${uid}-amt`}>
                About how much each time
              </label>
              <span className="st-money">
                <span aria-hidden="true">$</span>
                <input
                  id={`${uid}-amt`}
                  className="st-input"
                  inputMode="decimal"
                  autoComplete="off"
                  value={pd.amountText}
                  onChange={(e) => setPd({ ...pd, amountText: e.target.value.replace(/[^0-9.,]/g, '') })}
                />
              </span>
            </div>
            <div className="st-field">
              <span className="st-field-label">How often</span>
              <StOptions label="How often" value={pd.cadence} options={freqOptions} onChange={(v) => setPd({ ...pd, cadence: v })} grid />
            </div>
            <div className="st-field">
              <label className="st-field-label" htmlFor={`${uid}-next`}>
                When does the next one come?
              </label>
              <input id={`${uid}-next`} className="st-input st-date" type="date" value={pd.next} onChange={(e) => setPd({ ...pd, next: e.target.value })} />
            </div>
            <p className="st-live" aria-live="polite">
              That’s about <strong className="st-strong">{dollars(Math.round(amount * freqOf(pd.cadence).perMonth))}</strong> a month.
            </p>
            <DialogActions
              left={
                pd.mode === 'edit' ? (
                  <button type="button" className="st-btn st-btn-danger st-btn-lg" onClick={() => void remove()} disabled={busy}>
                    Delete
                  </button>
                ) : undefined
              }
              onCancel={() => setPd(null)}
              submitLabel={pd.mode === 'edit' ? 'Save' : 'Add'}
              submitDisabled={!ok}
              busy={busy}
            />
          </>
        )}
      </StDialog>
    </section>
  );
}
