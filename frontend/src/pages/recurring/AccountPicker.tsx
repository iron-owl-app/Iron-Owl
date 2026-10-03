import { useEffect, useId, useState, type FormEvent } from 'react';
import { api, errorMessage, type Account } from '../../api';
import { Icon } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import { Avatar, Money } from '../../components/ui';

/** "Change account": which checking account the calendar projects (PUT /api/forecast/account). */
export function AccountPicker({
  open,
  accounts,
  current,
  onClose,
  onSaved,
}: {
  open: boolean;
  accounts: Account[];
  current: number | null;
  onClose: () => void;
  onSaved: (name: string | null) => void;
}) {
  const uid = useId();
  const [sel, setSel] = useState<number | null>(current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setSel(current);
      setError(null);
    }
  }, [open, current]);
  const options = accounts.filter((a) => !a.hidden && a.category === 'bank');

  async function save(ev: FormEvent) {
    ev.preventDefault();
    if (sel === null) return;
    if (sel === current) return onClose();
    setBusy(true);
    setError(null);
    try {
      const f = await api.forecast.setAccount(sel);
      onSaved(f.account?.name ?? null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="Forecast account"
      subtitle="The calendar projects this account’s balance."
      onClose={onClose}
      busy={busy}
      width={460}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form={`${uid}-f`} className="btn btn-primary" disabled={busy || sel === null}>
            {busy ? 'Saving…' : 'Use this account'}
          </button>
        </>
      }
    >
      <form id={`${uid}-f`} onSubmit={save}>
        {error && (
          <div className="banner banner-error" role="alert">
            <Icon name="alert" />
            <div className="banner-body">{error}</div>
          </div>
        )}
        {options.length === 0 ? (
          <p className="small muted">No checking or savings accounts yet.</p>
        ) : (
          <fieldset className="acct-radios">
            <legend className="sr-only">Account</legend>
            {options.map((a) => (
              <label key={a.id} className={`acct-radio${sel === a.id ? ' is-on' : ''}`}>
                <input type="radio" name={`${uid}-acct`} checked={sel === a.id} onChange={() => setSel(a.id)} />
                <Avatar name={a.institution_name || a.name} size="sm" />
                <span className="acct-radio-name">
                  <span className="truncate">{a.name}</span>
                  <span className="row-sub">{[a.institution_name, a.mask ? `··${a.mask}` : null].filter(Boolean).join(' ')}</span>
                </span>
                <Money value={a.current_balance} className="acct-radio-bal" />
              </label>
            ))}
          </fieldset>
        )}
      </form>
    </Modal>
  );
}
