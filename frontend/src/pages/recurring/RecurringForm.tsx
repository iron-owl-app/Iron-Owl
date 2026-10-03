import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import type { Account, Cadence, RecurringInput, RecurringItem, ReminderDays, TxnCategory } from '../../api';
import { Icon } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import { parseMoneyInput } from '../../components/ui';
import { REMIND_OPTIONS } from './calLib';
import { EDIT_CADENCES } from './calMath';
import { budgetCategories } from './AddItemDialog';

/**
 * "Edit details": name, direction, amount, schedule (including Just once and Twice a month),
 * next date, account, reminder and budget category. Changes apply right away (with Undo).
 */
export function RecurringForm({
  item,
  accounts,
  categories,
  onClose,
  onSave,
}: {
  item: RecurringItem | null;
  accounts: Account[];
  categories: TxnCategory[];
  onClose: () => void;
  /** Resolves null on success, else the error to show. */
  onSave: (item: RecurringItem, body: RecurringInput) => Promise<string | null>;
}) {
  const open = item !== null;
  const uid = useId();
  const [name, setName] = useState('');
  const [dir, setDir] = useState<'out' | 'in'>('out');
  const [amount, setAmount] = useState('');
  const [cadence, setCadence] = useState<Cadence>('monthly');
  const [next, setNext] = useState('');
  const [accountId, setAccountId] = useState<string>('');
  const [remind, setRemind] = useState<ReminderDays>(0);
  const [cat, setCat] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!item) return;
    setName(item.name);
    setDir(item.amount > 0 ? 'in' : 'out');
    setAmount(String(Math.abs(item.amount)));
    setCadence(item.cadence);
    setNext(item.next_date);
    setAccountId(item.account_id !== null ? String(item.account_id) : '');
    setRemind(item.reminder_days);
    setCat(item.category_id ?? '');
    setErrors({});
    setFormError(null);
    requestAnimationFrame(() => nameRef.current?.focus());
  }, [item]);

  const visible = accounts.filter((a) => !a.hidden && (a.category === 'bank' || a.category === 'credit' || a.category === 'hsa'));
  const catOptions = budgetCategories(categories, dir);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    if (!item) return;
    const e: Record<string, string> = {};
    if (!name.trim()) e.name = 'Give it a name.';
    const amt = parseMoneyInput(amount);
    if (amt === null) e.amount = 'Enter the amount.';
    else if (Number.isNaN(amt) || amt <= 0) e.amount = 'Use a positive amount like 79.99';
    if (!/^\d{4}-\d{2}-\d{2}$/.test(next)) e.next = 'Pick the next date.';
    setErrors(e);
    if (Object.keys(e).length) return;
    setBusy(true);
    setFormError(null);
    const msg = await onSave(item, {
      name: name.trim(),
      amount: dir === 'in' ? amt! : -amt!,
      cadence,
      next_date: next,
      account_id: accountId ? Number(accountId) : null,
      reminder_days: remind,
      category_id: cat || null,
    });
    setBusy(false);
    if (msg) setFormError(msg);
  }

  const id = (k: string) => `${uid}-${k}`;
  const err = (k: string) =>
    errors[k] ? (
      <div className="field-error" id={id(`${k}-err`)}>
        {errors[k]}
      </div>
    ) : null;

  return (
    <Modal
      open={open}
      title={item ? `Edit “${item.name}”` : ''}
      subtitle="Changes apply to the calendar right away. You can undo them."
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form={id('form')} className="btn btn-primary" disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </>
      }
    >
      <form id={id('form')} className="a-form-grid" onSubmit={submit} noValidate>
        {formError && (
          <div className="banner banner-error a-span" role="alert">
            <Icon name="alert" />
            <div className="banner-body">{formError}</div>
          </div>
        )}
        <div className="field a-span">
          <label className="field-label" htmlFor={id('name')}>
            Name
          </label>
          <input
            ref={(el) => {
              nameRef.current = el;
              // The native dialog focuses [autofocus] when it opens (rAF alone can lose to it).
              el?.setAttribute('autofocus', '');
            }}
            id={id('name')}
            className="input"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setErrors((s) => ({ ...s, name: '' }));
            }}
            aria-invalid={errors.name ? true : undefined}
            aria-describedby={errors.name ? id('name-err') : undefined}
            autoComplete="off"
            maxLength={120}
          />
          {err('name')}
        </div>
        <div className="field">
          <span className="field-label" id={id('dir-l')}>
            Type
          </span>
          <div className="segmented a-seg" role="group" aria-labelledby={id('dir-l')}>
            <button type="button" aria-pressed={dir === 'out'} onClick={() => setDir('out')}>
              Money out
            </button>
            <button type="button" aria-pressed={dir === 'in'} onClick={() => setDir('in')}>
              Money in
            </button>
          </div>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={id('amount')}>
            Amount
          </label>
          <span className="money-input">
            <span className="money-prefix" aria-hidden="true">
              $
            </span>
            <input
              id={id('amount')}
              className="input"
              inputMode="decimal"
              value={amount}
              onChange={(e) => {
                setAmount(e.target.value);
                setErrors((s) => ({ ...s, amount: '' }));
              }}
              aria-invalid={errors.amount ? true : undefined}
              aria-describedby={errors.amount ? id('amount-err') : undefined}
              autoComplete="off"
            />
          </span>
          {err('amount')}
        </div>
        <div className="field">
          <label className="field-label" htmlFor={id('cadence')}>
            Repeats
          </label>
          <select id={id('cadence')} className="select" value={cadence} onChange={(e) => setCadence(e.target.value as Cadence)}>
            {EDIT_CADENCES.map(([c, label]) => (
              <option key={c} value={c}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={id('next')}>
            {cadence === 'once' ? 'Date' : 'Next date'}
          </label>
          <input
            id={id('next')}
            type="date"
            className="input"
            value={next}
            onChange={(e) => {
              setNext(e.target.value);
              setErrors((s) => ({ ...s, next: '' }));
            }}
            aria-invalid={errors.next ? true : undefined}
            aria-describedby={errors.next ? id('next-err') : undefined}
          />
          {err('next')}
        </div>
        <div className="field a-span">
          <label className="field-label" htmlFor={id('acct')}>
            Paid from
          </label>
          <select id={id('acct')} className="select" value={accountId} onChange={(e) => setAccountId(e.target.value)} aria-describedby={id('acct-hint')}>
            <option value="">No specific account</option>
            {visible.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
                {a.mask ? ` ··${a.mask}` : ''}
              </option>
            ))}
          </select>
          <div className="field-hint" id={id('acct-hint')}>
            Things charged to a credit card show on the calendar but don’t change checking; the card’s autopay does.
          </div>
        </div>
        <div className="field">
          <span className="field-label" id={id('rem-l')}>
            Remind me
          </span>
          <div className="segmented a-seg" role="group" aria-labelledby={id('rem-l')}>
            {REMIND_OPTIONS.map(([v, l]) => (
              <button key={v} type="button" aria-pressed={remind === v} onClick={() => setRemind(v)}>
                {l}
              </button>
            ))}
          </div>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={id('cat')}>
            Budget category
          </label>
          <select id={id('cat')} className="select" value={cat} onChange={(e) => setCat(e.target.value)}>
            <option value="">None</option>
            {catOptions.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
            {cat && !catOptions.some((c) => c.id === cat) && <option value={cat}>{item?.category_name ?? cat}</option>}
          </select>
        </div>
      </form>
    </Modal>
  );
}
