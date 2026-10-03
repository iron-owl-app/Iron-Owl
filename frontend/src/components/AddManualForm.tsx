import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { MANUAL_KINDS, type ManualDraft, type ManualErrors } from './manualAccount';

/**
 * "Add an account yourself": kind tiles, name, bank or lender, last 4 digits, the balance ("How
 * much you owe now" for debts), and for debts the interest rate and monthly payment. The window's
 * footer submits it (`form={formId}`).
 */
export function AddManualForm({
  formId,
  draft,
  onChange,
  onSubmit,
  errors,
  busy,
  formError,
}: {
  formId: string;
  draft: ManualDraft;
  onChange: (d: ManualDraft) => void;
  onSubmit: () => void;
  errors: ManualErrors;
  busy: boolean;
  formError: string | null;
}) {
  const uid = useId();
  const firstRef = useRef<HTMLButtonElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const kind = MANUAL_KINDS.find((k) => k.kind === draft.kind);
  const debt = !!kind?.debt;
  const negative = !!kind?.negative;
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    // rAF: inside a modal <dialog>, the dialog opens after this effect runs.
    const h = requestAnimationFrame(() => firstRef.current?.focus());
    return () => cancelAnimationFrame(h);
  }, []);

  const set = <K extends keyof ManualDraft>(key: K, value: ManualDraft[K]) => onChange({ ...draft, [key]: value });
  const err = (k: keyof ManualDraft) => (touched ? errors[k] : undefined);
  const described = (k: keyof ManualDraft) => (err(k) ? `${uid}-${k}-err` : undefined);
  const errLine = (k: keyof ManualDraft) =>
    err(k) ? (
      <span className="amf-error" id={`${uid}-${k}-err`}>
        {err(k)}
      </span>
    ) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    onSubmit();
    // Something to fix: take the user to the first field that says so.
    requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus());
  }

  return (
    <form ref={formRef} id={formId} className="amf" onSubmit={submit} noValidate>
      <div className="amf-field">
        <span className="amf-label" id={`${uid}-kind`}>
          What kind?
        </span>
        <div className="amf-kinds" role="radiogroup" aria-labelledby={`${uid}-kind`} aria-describedby={described('kind')}>
          {MANUAL_KINDS.map((k, i) => {
            const on = draft.kind === k.kind;
            return (
              <button
                key={k.kind}
                ref={i === 0 ? firstRef : undefined}
                type="button"
                role="radio"
                aria-checked={on}
                className={`amf-kind${on ? ' is-on' : ''}${k.kind === 'other' ? ' is-wide' : ''}`}
                onClick={() => set('kind', k.kind)}
                disabled={busy}
              >
                {k.label}
              </button>
            );
          })}
        </div>
        {errLine('kind')}
      </div>

      <label className="amf-field">
        <span className="amf-label">Name</span>
        <input
          className="amf-input"
          value={draft.name}
          maxLength={100}
          placeholder={`Like “${kind?.example ?? 'Federal student loan'}”`}
          onChange={(e) => set('name', e.target.value)}
          aria-invalid={!!err('name') || undefined}
          aria-describedby={described('name')}
          disabled={busy}
        />
        {errLine('name')}
      </label>

      <div className="amf-grid">
        <label className="amf-field">
          <span className="amf-label">Bank or lender</span>
          <input className="amf-input" value={draft.bank} maxLength={100} onChange={(e) => set('bank', e.target.value)} disabled={busy} />
        </label>
        <label className="amf-field">
          <span className="amf-label">Last 4 digits (optional)</span>
          <input
            className="amf-input"
            inputMode="numeric"
            maxLength={4}
            autoComplete="off"
            value={draft.last4}
            onChange={(e) => set('last4', e.target.value.replace(/\D/g, '').slice(0, 4))}
            aria-invalid={!!err('last4') || undefined}
            aria-describedby={described('last4')}
            disabled={busy}
          />
          {errLine('last4')}
        </label>
        <label className="amf-field">
          <span className="amf-label">{debt ? 'How much you owe now' : 'Balance now'}</span>
          <span className="amf-affix">
            <span className="amf-sign" aria-hidden="true">
              $
            </span>
            <input
              className="amf-input amf-input-money"
              inputMode="decimal"
              autoComplete="off"
              value={draft.balance}
              onChange={(e) => set('balance', e.target.value.replace(negative ? /[^0-9.,\-−]/g : /[^0-9.,]/g, ''))}
              aria-invalid={!!err('balance') || undefined}
              aria-describedby={[described('balance'), negative ? `${uid}-bal-hint` : undefined].filter(Boolean).join(' ') || undefined}
              disabled={busy}
            />
          </span>
          {negative && (
            <span className="amf-hint" id={`${uid}-bal-hint`}>
              Overdrawn? Type a minus sign first, like -50.00
            </span>
          )}
          {errLine('balance')}
        </label>
        {debt && (
          <label className="amf-field">
            <span className="amf-label">Interest rate (optional)</span>
            <span className="amf-affix">
              <input
                className="amf-input amf-input-pct"
                inputMode="decimal"
                autoComplete="off"
                value={draft.rate}
                onChange={(e) => set('rate', e.target.value.replace(/[^0-9.]/g, ''))}
                aria-invalid={!!err('rate') || undefined}
                aria-describedby={described('rate')}
                disabled={busy}
              />
              <span className="amf-pct" aria-hidden="true">
                %
              </span>
            </span>
            {errLine('rate')}
          </label>
        )}
        {debt && (
          <label className="amf-field">
            <span className="amf-label">Monthly payment (optional)</span>
            <span className="amf-affix">
              <span className="amf-sign" aria-hidden="true">
                $
              </span>
              <input
                className="amf-input amf-input-money"
                inputMode="decimal"
                autoComplete="off"
                value={draft.payment}
                onChange={(e) => set('payment', e.target.value.replace(/[^0-9.,]/g, ''))}
                aria-invalid={!!err('payment') || undefined}
                aria-describedby={described('payment')}
                disabled={busy}
              />
            </span>
            {errLine('payment')}
          </label>
        )}
      </div>
      {formError && (
        <p className="amf-error" role="alert">
          {formError}
        </p>
      )}
    </form>
  );
}
