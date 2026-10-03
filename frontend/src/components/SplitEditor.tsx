import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react';
import { api, errorMessage, type CategoryKind, type SplitInput, type Transaction, type TxnCategory } from '../api';
import { formatDateSmart, formatMoney } from '../lib/format';
import { Icon } from './Icon';
import { Modal } from './Modal';
import { parseMoneyInput } from './ui';
import { useToast } from './Toast';

const MAX_PARTS = 20;
const KIND_GROUPS: [CategoryKind, string][] = [
  ['spending', 'Spending'],
  ['income', 'Income'],
  ['transfer', 'Transfers'],
  ['fixed', 'Fixed costs'],
];

type Row = { key: number; amount: string; category: string; notes: string };

const toCents = (n: number) => Math.round(n * 100);

/** Amount text for an input: magnitude only (the sign always follows the transaction). */
const amountText = (n: number) => Math.abs(n).toFixed(2);

/**
 * Split one transaction across categories (SPEC Release 3 §3). Parts are typed as
 * positive amounts; they take the transaction's sign. Saving needs 2–20 parts that
 * add up to the transaction exactly.
 */
export function SplitEditor({
  txn,
  categories,
  onClose,
  onSaved,
}: {
  txn: Transaction;
  categories: TxnCategory[];
  onClose: () => void;
  onSaved: (t: Transaction) => void;
}) {
  const uid = useId();
  const toast = useToast();
  const nextKey = useRef(1);
  const newRow = (p: Partial<Row> = {}): Row => ({ key: nextKey.current++, amount: '', category: '', notes: '', ...p });
  const [rows, setRows] = useState<Row[]>(() =>
    txn.splits.length
      ? txn.splits.map((s) => newRow({ amount: amountText(s.amount), category: s.category, notes: s.notes ?? '' }))
      : [newRow({ category: txn.category ?? 'OTHER' }), newRow()],
  );
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState<'save' | 'remove' | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const firstInput = useRef<HTMLInputElement>(null);
  const focusKey = useRef<number | null>(null);

  useEffect(() => {
    firstInput.current?.focus();
  }, []);

  // After "Add part", move focus to the new row's amount.
  useEffect(() => {
    if (focusKey.current === null) return;
    document.getElementById(`${uid}-amt-${focusKey.current}`)?.focus();
    focusKey.current = null;
  });

  const sign = txn.amount < 0 ? -1 : 1;
  const totalCents = toCents(Math.abs(txn.amount));
  const direction = txn.amount < 0 ? 'money out' : 'money in';

  const parsed = useMemo(
    () =>
      rows.map((r) => {
        const v = parseMoneyInput(r.amount);
        let error: string | null = null;
        if (v === null) error = 'Enter an amount.';
        else if (Number.isNaN(v)) error = 'Use a number like 12.50.';
        else if (v < 0) error = `Use a positive amount. Every part is ${direction}, like the transaction.`;
        else if (toCents(v) === 0) error = 'Use an amount more than 0.';
        return { cents: v !== null && !Number.isNaN(v) ? toCents(v) : 0, amountError: error, categoryError: r.category ? null : 'Pick a category.' };
      }),
    [rows, direction],
  );
  const allocated = parsed.reduce((s, p) => s + p.cents, 0);
  const remaining = totalCents - allocated;
  const rowsValid = parsed.every((p) => !p.amountError && !p.categoryError);
  const valid = rowsValid && remaining === 0 && rows.length >= 2 && rows.length <= MAX_PARTS;

  const update = (key: number, patch: Partial<Row>) => {
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
    setFormError(null);
  };

  function addRow() {
    if (rows.length >= MAX_PARTS) return;
    const r = newRow({ amount: remaining > 0 ? (remaining / 100).toFixed(2) : '' });
    focusKey.current = r.key;
    setRows((rs) => [...rs, r]);
  }

  function removeRow(key: number, index: number) {
    setRows((rs) => rs.filter((r) => r.key !== key));
    // Keep focus inside the editor: the previous row's remove button, else the next one.
    requestAnimationFrame(() => {
      const btns = document.querySelectorAll<HTMLButtonElement>(`[data-split-remove="${uid}"]`);
      (btns[Math.max(0, index - 1)] ?? firstInput.current)?.focus();
    });
  }

  /**
   * Balance the split in one click: what's left goes on the last part without an amount
   * (else the last part); an overage comes off the last part big enough to absorb it.
   */
  const fillTarget = useMemo(() => {
    if (remaining > 0) return [...rows].reverse().find((r) => !r.amount.trim()) ?? rows[rows.length - 1] ?? null;
    if (remaining < 0) return [...rows].reverse().find((r) => (parsed[rows.indexOf(r)]?.cents ?? 0) > -remaining && !parsed[rows.indexOf(r)]?.amountError) ?? null;
    return null;
  }, [rows, parsed, remaining]);

  function fillRemaining() {
    if (!fillTarget) return;
    const p = parsed[rows.indexOf(fillTarget)];
    const current = p && !p.amountError ? p.cents : 0;
    const next = current + remaining;
    if (next <= 0) return;
    update(fillTarget.key, { amount: (next / 100).toFixed(2) });
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setTried(true);
    if (!valid) {
      setFormError(
        !rowsValid
          ? 'Fix the highlighted parts.'
          : remaining > 0
            ? `${formatMoney(remaining / 100)} still needs a category.`
            : `The parts are ${formatMoney(-remaining / 100)} more than the transaction.`,
      );
      return;
    }
    const splits: SplitInput[] = rows.map((r, i) => ({ amount: (sign * parsed[i]!.cents) / 100, category: r.category, notes: r.notes.trim() || null }));
    setBusy('save');
    setFormError(null);
    try {
      const u = await api.setSplits(txn.id, splits);
      onSaved(u);
      toast.push({ tone: 'success', title: txn.splits.length ? 'Split updated' : 'Transaction split', body: `${txn.merchant_name || txn.name} now counts in ${rows.length} categories.`, timeout: 3500 });
      onClose();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function removeSplit() {
    setBusy('remove');
    setFormError(null);
    try {
      const u = await api.setSplits(txn.id, []);
      onSaved(u);
      toast.push({ tone: 'info', title: 'Split removed', body: `${txn.merchant_name || txn.name} counts as ${u.category_name} again.`, timeout: 3500 });
      onClose();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const title = txn.merchant_name || txn.name;
  const status = remaining === 0 ? 'done' : remaining > 0 ? 'left' : 'over';

  return (
    <Modal
      open
      title={txn.splits.length ? 'Edit split' : 'Split transaction'}
      subtitle={
        <>
          {title} · {formatDateSmart(txn.date)} · <span className="num">{formatMoney(txn.amount)}</span>
        </>
      }
      onClose={onClose}
      busy={busy !== null}
      width={640}
      footer={
        <>
          {txn.splits.length > 0 && (
            <button type="button" className="btn btn-danger-quiet split-remove-all" onClick={() => void removeSplit()} disabled={busy !== null}>
              {busy === 'remove' ? 'Removing…' : 'Remove split'}
            </button>
          )}
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy !== null}>
            Cancel
          </button>
          <button type="submit" form={`${uid}-form`} className="btn btn-primary" disabled={busy !== null} aria-describedby={`${uid}-remaining`}>
            {busy === 'save' ? 'Saving…' : 'Save split'}
          </button>
        </>
      }
    >
      <form id={`${uid}-form`} className="split-form" onSubmit={save} noValidate>
        <p className="small muted split-intro">
          Each part counts in its own category in budgets, reports and exports. Enter amounts as positive numbers; every part is {direction}, like
          the transaction.
        </p>

        <div className="split-head" aria-hidden="true">
          <span>Amount</span>
          <span>Category</span>
          <span>Note (optional)</span>
        </div>
        <ol className="split-rows">
          {rows.map((r, i) => {
            const p = parsed[i]!;
            const amtErr = tried ? p.amountError : null;
            const catErr = tried ? p.categoryError : null;
            const n = i + 1;
            return (
              <li key={r.key} className="split-row">
                <div className="input-affix split-amt">
                  <span className="affix">$</span>
                  <input
                    ref={i === 0 ? firstInput : undefined}
                    id={`${uid}-amt-${r.key}`}
                    className="input has-prefix num"
                    inputMode="decimal"
                    autoComplete="off"
                    value={r.amount}
                    placeholder="0.00"
                    onChange={(e) => update(r.key, { amount: e.target.value })}
                    onBlur={() => {
                      const v = parseMoneyInput(r.amount);
                      if (v !== null && !Number.isNaN(v) && v > 0) update(r.key, { amount: v.toFixed(2) });
                    }}
                    aria-label={`Part ${n} amount`}
                    aria-invalid={amtErr ? true : undefined}
                    aria-describedby={amtErr ? `${uid}-err-${r.key}` : undefined}
                  />
                </div>
                <select
                  className="select split-cat"
                  value={r.category}
                  onChange={(e) => update(r.key, { category: e.target.value })}
                  aria-label={`Part ${n} category`}
                  aria-invalid={catErr ? true : undefined}
                  aria-describedby={catErr ? `${uid}-err-${r.key}` : undefined}
                >
                  <option value="" disabled>
                    Choose…
                  </option>
                  {KIND_GROUPS.map(([k, label]) => {
                    const list = categories.filter((c) => c.kind === k && (!c.hidden || c.id === r.category));
                    return list.length ? (
                      <optgroup key={k} label={label}>
                        {list.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name}
                          </option>
                        ))}
                      </optgroup>
                    ) : null;
                  })}
                </select>
                <input
                  className="input split-note"
                  value={r.notes}
                  maxLength={200}
                  placeholder="Note"
                  onChange={(e) => update(r.key, { notes: e.target.value })}
                  aria-label={`Part ${n} note`}
                />
                <button
                  type="button"
                  className="icon-btn icon-btn-danger split-row-remove"
                  onClick={() => removeRow(r.key, i)}
                  disabled={rows.length <= 2}
                  aria-label={`Remove part ${n}`}
                  title={rows.length <= 2 ? 'A split needs at least 2 parts' : 'Remove this part'}
                  data-split-remove={uid}
                >
                  <Icon name="trash" />
                </button>
                {(amtErr || catErr) && (
                  <p className="field-error split-row-err" id={`${uid}-err-${r.key}`}>
                    {[amtErr, catErr].filter(Boolean).join(' ')}
                  </p>
                )}
              </li>
            );
          })}
        </ol>

        <div className="split-foot">
          <button type="button" className="btn btn-sm" onClick={addRow} disabled={rows.length >= MAX_PARTS}>
            <Icon name="plus" />
            Add part
          </button>
          <div className={`split-remaining is-${status}`} id={`${uid}-remaining`} aria-live="polite">
            {status === 'done' ? (
              <>
                <Icon name="check" />
                <span>
                  Remaining <strong className="num">{formatMoney(0)}</strong>
                </span>
              </>
            ) : status === 'left' ? (
              <span>
                Remaining <strong className="num">{formatMoney(remaining / 100)}</strong>
              </span>
            ) : (
              <span>
                Over by <strong className="num">{formatMoney(-remaining / 100)}</strong>
              </span>
            )}
          </div>
          {fillTarget && (
            <button type="button" className="link-btn split-fill" onClick={fillRemaining}>
              {remaining > 0 ? `Put ${formatMoney(remaining / 100)} on part ${rows.indexOf(fillTarget) + 1}` : `Take ${formatMoney(-remaining / 100)} off part ${rows.indexOf(fillTarget) + 1}`}
            </button>
          )}
        </div>
        <p className="xsmall subtle">
          Total <span className="num">{formatMoney(Math.abs(txn.amount))}</span> · {rows.length} of {MAX_PARTS} parts
        </p>

        {formError && (
          <p className="field-error" role="alert">
            {formError}
          </p>
        )}
      </form>
    </Modal>
  );
}
