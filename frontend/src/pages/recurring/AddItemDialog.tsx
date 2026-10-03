import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { api, type Account, type Cadence, type RecurringInput, type ReminderDays, type TxnCategory } from '../../api';
import { Modal } from '../../components/Modal';
import { Icon } from '../../components/Icon';
import { parseMoneyInput } from '../../components/ui';
import { REMIND_OPTIONS, md } from './calLib';
import { ADD_CADENCES, ADD_KINDS, MAX_AMOUNT, TOO_LARGE, addPreview, type AddKind } from './calMath';
import './recurring.css';

type Dir = 'out' | 'in';

/** Budget categories a bill or paycheck can belong to, for the direction. */
export function budgetCategories(cats: TxnCategory[], dir: Dir): TxnCategory[] {
  const kinds = dir === 'in' ? ['income'] : ['spending', 'fixed'];
  return cats.filter((c) => !c.hidden && kinds.includes(c.kind) && c.id !== 'OTHER').sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * "Add a bill or paycheck" (recurring-v2 design §9; centered Modal, 540px): three type tiles
 * (Bill · Paycheck or income · Card charge), name, amount, first date, how often, a live
 * preview line, and "More options" (which card, reminder, budget category). The account
 * follows the type: checking for bills and income, a credit card for card charges.
 * Also opened from Budget's "+ Add a bill" with the category already chosen.
 */
export function AddItemDialog({
  open,
  date,
  today,
  horizonEnd,
  checkingId,
  cards,
  categories,
  initialCategory = null,
  initialKind = 'out',
  dayBalance,
  onClose,
  onAdd,
}: {
  open: boolean;
  date: string;
  today: string;
  horizonEnd: string;
  /** The forecast (checking) account. */
  checkingId: number | null;
  cards: Account[];
  categories: TxnCategory[];
  /** Budget "+ Add a bill": the category is already chosen (money out). */
  initialCategory?: string | null;
  /** The type tile picked when it opens (e.g. "Add your paycheck" opens on Paycheck). */
  initialKind?: AddKind;
  /** Projected end-of-day checking balance on a date, for the preview (omit to leave it out). */
  dayBalance?: (date: string) => number | null;
  onClose: () => void;
  /** Resolves null on success, else the error to show. */
  onAdd: (input: RecurringInput) => Promise<string | null>;
}) {
  const uid = useId();
  const id = (k: string) => `${uid}-${k}`;
  const [kind, setKind] = useState<AddKind>(initialKind);
  const [name, setName] = useState('');
  const [amount, setAmount] = useState('');
  const [first, setFirst] = useState(date);
  const [cardId, setCardId] = useState<string>('');
  const [rep, setRep] = useState<Cadence>('monthly');
  const [remind, setRemind] = useState<ReminderDays>(0);
  const [cat, setCat] = useState('');
  const [catTouched, setCatTouched] = useState(false);
  const [more, setMore] = useState(false);
  const [guessing, setGuessing] = useState(false);
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement | null>(null);
  const dir: Dir = kind === 'in' ? 'in' : 'out';
  const hasCards = cards.length > 0;

  useEffect(() => {
    if (!open) return;
    setKind(initialCategory ? 'out' : initialKind === 'card' && !hasCards ? 'out' : initialKind);
    setName('');
    setAmount('');
    setFirst(date);
    setCardId(cards[0] ? String(cards[0].id) : '');
    setRep('monthly');
    setRemind(0);
    setCat(initialCategory ?? '');
    setCatTouched(!!initialCategory);
    setMore(!!initialCategory);
    setTouched({});
    setFormError(null);
    requestAnimationFrame(() => nameRef.current?.focus());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, date, initialCategory, initialKind]);

  const options = budgetCategories(categories, dir);

  // Guess the budget category from past transactions with this name (until the user picks one).
  useEffect(() => {
    // Opened from a budget category ("+ Add a bill"): that category stays unless the user changes it.
    if (!open || catTouched || initialCategory) return;
    const q = name.trim();
    if (q.length < 3) {
      setCat(dir === 'in' && options.some((c) => c.id === 'INCOME') ? 'INCOME' : '');
      return;
    }
    let live = true;
    const h = window.setTimeout(async () => {
      setGuessing(true);
      try {
        // Only the sign goes along (the server matches money in vs out), never the amount.
        const g = await api.recurring.categoryGuess({ name: q, amount_sign: dir === 'in' ? 1 : -1 });
        if (!live) return;
        const allowed = new Set(options.map((c) => c.id));
        setCat(g.category_id && allowed.has(g.category_id) ? g.category_id : dir === 'in' && allowed.has('INCOME') ? 'INCOME' : '');
      } catch {
        /* a guess is optional */
      } finally {
        if (live) setGuessing(false);
      }
    }, 450);
    return () => {
      live = false;
      window.clearTimeout(h);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, dir, open, catTouched, categories, initialCategory]);

  const amt = parseMoneyInput(amount);
  const dateOk = /^\d{4}-\d{2}-\d{2}$/.test(first);
  const errors: Record<string, string> = {};
  if (!name.trim()) errors.name = 'Give it a name.';
  if (amt === null) errors.amount = 'Type an amount, like 25.';
  else if (Number.isNaN(amt) || amt <= 0) errors.amount = 'Use a positive amount, like 25.';
  else if (amt > MAX_AMOUNT) errors.amount = TOO_LARGE;
  if (!dateOk) errors.first = 'Pick the date.';
  else if (first < today) errors.first = 'Pick today or a later date.';
  else if (first > horizonEnd) errors.first = `Pick a date by ${md(horizonEnd)}.`;
  const onCard = kind === 'card';
  if (onCard && !cardId) errors.card = 'Pick the card.';
  const invalid = Object.keys(errors).length > 0;
  const err = (k: string) =>
    touched[k] && errors[k] ? (
      <div className="field-error" id={id(`${k}-err`)}>
        {errors[k]}
      </div>
    ) : null;
  const described = (k: string) => (touched[k] && errors[k] ? id(`${k}-err`) : undefined);

  const preview = errors.first && dateOk
    ? errors.first
    : addPreview({
        name,
        amount: amt,
        kind,
        cadence: rep,
        date: first,
        today,
        dayBalance: dayBalance && dateOk ? dayBalance(first) : null,
      });

  async function submit(e: FormEvent) {
    e.preventDefault();
    setTouched({ name: true, amount: true, first: true, card: true });
    if (errors.card) setMore(true);
    if (invalid || busy) return;
    const account = onCard ? Number(cardId) : checkingId;
    setBusy(true);
    setFormError(null);
    const msg = await onAdd({
      name: name.trim(),
      amount: dir === 'in' ? amt! : -amt!,
      cadence: rep,
      next_date: first,
      account_id: account,
      reminder_days: remind,
      category_id: cat || null,
    });
    setBusy(false);
    if (msg) setFormError(msg);
  }

  const kindInfo = ADD_KINDS.find((k) => k.kind === kind)!;

  return (
    <Modal
      open={open}
      title="Add a bill or paycheck"
      subtitle="It goes on the calendar and into your balance."
      onClose={onClose}
      busy={busy}
      width={540}
      className={`cal-add-dialog cal-k-${kind}`}
      footer={
        <>
          <button type="submit" form={id('form')} className="btn btn-primary" disabled={busy || invalid} aria-describedby={id('preview')}>
            {busy ? 'Adding…' : kind === 'in' ? 'Add income' : 'Add bill'}
          </button>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
        </>
      }
    >
      <form id={id('form')} className="cal-form" onSubmit={submit} noValidate>
        {formError && (
          <div className="banner banner-error" role="alert">
            <Icon name="alert" />
            <div className="banner-body">{formError}</div>
          </div>
        )}
        <div className="cal-kinds" role="group" aria-label="Type">
          {ADD_KINDS.map((k) => {
            const off = (k.kind === 'card' && !hasCards) || (!!initialCategory && k.kind === 'in');
            return (
              <button
                key={k.kind}
                type="button"
                aria-pressed={kind === k.kind}
                className={`cal-kind cal-k-${k.kind}`}
                disabled={off}
                onClick={() => {
                  setKind(k.kind);
                  if (!catTouched) setCat('');
                }}
              >
                <span className="cal-kind-name">{k.label}</span>
                <span className="cal-kind-hint">{k.kind === 'card' && !hasCards ? 'No credit card added' : k.hint}</span>
              </button>
            );
          })}
        </div>
        <div className="cal-form-name">
          <div className="field">
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
              onChange={(e) => setName(e.target.value)}
              onBlur={() => setTouched((t) => ({ ...t, name: true }))}
              placeholder={kindInfo.placeholder}
              autoComplete="off"
              maxLength={120}
              aria-invalid={touched.name && errors.name ? true : undefined}
              aria-describedby={described('name')}
            />
            {err('name')}
          </div>
          <div className="field">
            <label className="field-label" htmlFor={id('amount')}>
              Amount
            </label>
            <span className="money-input cal-amount">
              <span className="money-prefix" aria-hidden="true">
                $
              </span>
              <input
                id={id('amount')}
                className="input"
                inputMode="decimal"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, amount: true }))}
                autoComplete="off"
                aria-invalid={touched.amount && errors.amount ? true : undefined}
                aria-describedby={described('amount')}
              />
            </span>
            {err('amount')}
          </div>
        </div>
        <div className="cal-form-2">
          <div className="field">
            <label className="field-label" htmlFor={id('first')}>
              {rep === 'once' ? 'Date' : 'First date'}
            </label>
            <input
              id={id('first')}
              type="date"
              className="input"
              value={first}
              min={today}
              max={horizonEnd}
              onChange={(e) => {
                setFirst(e.target.value);
                setTouched((t) => ({ ...t, first: true }));
              }}
              aria-invalid={touched.first && errors.first ? true : undefined}
              aria-describedby={described('first')}
            />
            {err('first')}
          </div>
          <div className="field">
            <label className="field-label" htmlFor={id('rep')}>
              How often
            </label>
            <select id={id('rep')} className="select" value={rep} onChange={(e) => setRep(e.target.value as Cadence)}>
              {ADD_CADENCES.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </div>
        </div>

        <p className={`cal-preview${invalid ? ' is-incomplete' : ''}`} id={id('preview')} aria-live="polite">
          {preview}
        </p>

        <details className="cal-more" open={more} onToggle={(e) => setMore((e.currentTarget as HTMLDetailsElement).open)}>
          <summary>More options</summary>
          <div className="cal-more-body">
            {onCard && cards.length > 1 && (
              <div className="field">
                <label className="field-label" htmlFor={id('card')}>
                  Which card?
                </label>
                <select id={id('card')} className="select" value={cardId} onChange={(e) => setCardId(e.target.value)} aria-describedby={described('card')}>
                  {cards.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                      {c.mask ? ` ··${c.mask}` : ''}
                    </option>
                  ))}
                </select>
                {err('card')}
              </div>
            )}
            {onCard && cards.length === 1 && (
              <p className="field-hint">
                Charged to {cards[0]!.name}
                {cards[0]!.mask ? ` ··${cards[0]!.mask}` : ''}.
              </p>
            )}
            <div className="field">
              <span className="field-label" id={id('rem-l')}>
                Remind me
              </span>
              <div className="segmented cal-seg" role="group" aria-labelledby={id('rem-l')}>
                {REMIND_OPTIONS.map(([v, l]) => (
                  <button key={v} type="button" aria-pressed={remind === v} onClick={() => setRemind(v)}>
                    {l}
                  </button>
                ))}
              </div>
            </div>
            <div className="field">
              <label className="field-label" htmlFor={id('cat')}>
                Which budget category?
              </label>
              <select
                id={id('cat')}
                className="select"
                value={cat}
                onChange={(e) => {
                  setCat(e.target.value);
                  setCatTouched(true);
                }}
                aria-describedby={id('cat-hint')}
              >
                <option value="">None</option>
                {options.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <div className="field-hint" id={id('cat-hint')} aria-live="polite">
                {guessing ? 'Looking at your past transactions…' : cat && !catTouched ? 'Our best guess from your past transactions. Change it if it’s wrong.' : 'It also shows under this category on your budget.'}
              </div>
            </div>
          </div>
        </details>
      </form>
    </Modal>
  );
}
