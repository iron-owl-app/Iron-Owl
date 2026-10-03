import { useCallback, useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import type { PlaidLinkError } from 'react-plaid-link';
import { api, ApiError, errorMessage, type Account, type Category, type DiscoveredAccount, type ItemKind, type PlaidItem } from '../api';
import { useApp } from '../state';
import { formatMoney, plural } from '../lib/format';
import { Icon, type IconName } from './Icon';
import { Modal } from './Modal';
import { ConfirmDialog } from './ConfirmDialog';
import { PlaidLinkLauncher } from './PlaidLinkLauncher';
import { AddManualForm } from './AddManualForm';
import { checkManual, EMPTY_MANUAL, manualReady, remindDays, type ManualDraft } from './manualAccount';
import { useToast } from './Toast';
import { allUsedText, slotsFull, slotsOf, type BankSlots } from './bankSlots';
import './addAccount.css';

/**
 * The "Add account" window (Release 3.10, design `Accounts Beginner.dc.html`):
 *   choose (Connect a bank · Add one yourself) → kind (Bank or card / 401(k), HSA or brokerage /
 *   Loan) → connecting → pick → (import)   or   choose → manual.
 * The kind step stays because Plaid Link's bank list depends on it. No institution-search step:
 * Plaid Link has its own. The same pick step serves "Manage accounts" and "Finish setup" for an
 * existing Item (Settings).
 */

export const KIND_OPTIONS: { kind: ItemKind; label: string; desc: string; icon: IconName; hue: number }[] = [
  { kind: 'bank', label: 'Bank or credit card', desc: 'Checking, savings, credit cards', icon: 'bank', hue: 250 },
  { kind: 'investment', label: '401(k), HSA or brokerage', desc: 'Retirement, HSA and brokerage holdings', icon: 'trend', hue: 295 },
  { kind: 'loan', label: 'Loan', desc: 'Mortgage, student loan, auto loan', icon: 'house', hue: 55 },
];

/** Singular type names used in the modal (design CAT_NAME). */
export const TYPE_NAME: Record<Category, string> = {
  bank: 'Bank account',
  hsa: 'HSA',
  retirement: 'Retirement account',
  investment: 'Investment account',
  other: 'Other asset',
  credit: 'Credit card',
  loan: 'Loan',
};

export type AddAccountStart =
  | { step: 'choose' }
  | { step: 'manual' }
  /** Skip "choose" and open Plaid Link for this kind (Settings' link cards). */
  | { step: 'link'; kind: ItemKind }
  /** Pick step for an existing Item: "Manage accounts" (ok items) or "Finish setup" (pending). */
  | { step: 'manage'; item: PlaidItem };

export type AddAccountResult = { type: 'manual'; account: Account } | { type: 'linked'; item: PlaidItem };

type Step = 'choose' | 'kind' | 'connecting' | 'pick' | 'manual';
type PickMode = 'new' | 'finish' | 'manage';
type Notice = { title: string; body: string; tone: 'error' | 'info' };

export function AddAccountModal({
  open,
  start = { step: 'choose' },
  onClose,
  onDone,
  onAdded,
}: {
  open: boolean;
  start?: AddAccountStart;
  onClose: () => void;
  onDone?: (r: AddAccountResult) => void;
  /** A manual account was added: the caller says so (with Undo). Without it, a plain toast does. */
  onAdded?: (a: Account) => void;
}) {
  const toast = useToast();
  const { plaid } = useApp();
  const [plaidConfigured, setPlaidConfigured] = useState<boolean | null>(plaid?.configured ?? null);
  const [step, setStep] = useState<Step>('choose');
  const [kind, setKind] = useState<ItemKind>('bank');
  const [phase, setPhase] = useState<'token' | 'link' | 'exchange' | 'loading'>('token');
  const [linkToken, setLinkToken] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [item, setItem] = useState<PlaidItem | null>(null);
  const [found, setFound] = useState<DiscoveredAccount[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [pickMode, setPickMode] = useState<PickMode>('new');
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [manual, setManual] = useState<ManualDraft>(EMPTY_MANUAL);
  /** "You've used N of 10 bank connections" (lifetime count from GET /api/plaid/status). */
  const [slots, setSlots] = useState<BankSlots | null>(null);
  const formId = useId();
  const startedFor = useRef<AddAccountStart | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (plaid) setPlaidConfigured(plaid.configured);
  }, [plaid]);

  // Learn whether Plaid is configured the first time the modal opens (if the app doesn't know yet).
  useEffect(() => {
    if (!open || plaidConfigured !== null) return;
    let live = true;
    api.plaid
      .status()
      .then((s) => live && setPlaidConfigured(s.configured))
      .catch(() => live && setPlaidConfigured(false));
    return () => {
      live = false;
    };
  }, [open, plaidConfigured]);

  const beginLink = useCallback(async (k: ItemKind) => {
    setKind(k);
    setNotice(null);
    setStep('connecting');
    setPhase('token');
    try {
      // Every way in (Accounts, Settings › Banks, Home) checks the plan's connections first:
      // when all are used, the kind step says so instead of opening Plaid Link.
      const s = slotsOf(await api.plaid.status());
      setSlots(s);
      if (slotsFull(s)) {
        setStep('kind');
        return;
      }
      const { link_token } = await api.plaid.linkToken(k);
      setLinkToken(link_token);
      setPhase('link');
    } catch (e) {
      setStep('kind');
      if (e instanceof ApiError && e.status === 503) {
        setPlaidConfigured(false);
        setNotice({ tone: 'info', title: 'Bank connections aren’t set up yet', body: 'The Plaid keys go in Settings › Banks. You can still add an account yourself.' });
      } else {
        setNotice({ tone: 'error', title: 'Couldn’t open the bank sign-in', body: errorMessage(e) });
      }
    }
  }, []);

  const loadManage = useCallback(async (it: PlaidItem) => {
    setItem(it);
    setPickMode(it.status === 'pending' ? 'finish' : 'manage');
    setStep('connecting');
    setPhase('loading');
    setNotice(null);
    try {
      const accounts = await api.plaid.discoveredAccounts(it.id);
      setFound(accounts);
      setSelected(Object.fromEntries(accounts.map((a) => [a.plaid_account_id, it.status === 'pending' ? true : a.imported])));
      setStep('pick');
    } catch (e) {
      toast.push({ tone: 'error', title: `Couldn’t load accounts from ${it.institution_name ?? 'Plaid'}`, body: errorMessage(e) });
      onClose();
    }
  }, [toast, onClose]);

  // Reset whenever the modal opens.
  useEffect(() => {
    if (!open) {
      startedFor.current = null;
      return;
    }
    if (startedFor.current === start) return;
    startedFor.current = start;
    setNotice(null);
    setFormError(null);
    setItem(null);
    setFound([]);
    setSelected({});
    setLinkToken(null);
    setBusy(false);
    setManual(EMPTY_MANUAL);
    if (start.step === 'manual') setStep('manual');
    else if (start.step === 'link') void beginLink(start.kind);
    else if (start.step === 'manage') void loadManage(start.item);
    else setStep('choose');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, start]);

  // The kind step: how many of the plan's bank connections are used (read fresh each time).
  useEffect(() => {
    if (!open || step !== 'kind') return;
    let live = true;
    api.plaid
      .status()
      .then((s) => {
        if (!live) return;
        setPlaidConfigured(s.configured);
        setSlots(slotsOf(s));
      })
      .catch(() => live && setSlots(null));
    return () => {
      live = false;
    };
  }, [open, step]);

  // Move focus into each step as it appears (the dialog may have just re-opened after Plaid Link).
  const visible = open && linkToken === null;
  useEffect(() => {
    if (!visible || step === 'manual' || step === 'connecting') return;
    const h = requestAnimationFrame(() => {
      const el = bodyRef.current?.querySelector<HTMLElement>('.aa-check:not(:disabled), .aa-tile:not(:disabled), .aa-option:not(:disabled)');
      el?.focus();
    });
    return () => cancelAnimationFrame(h);
  }, [visible, step]);

  // ------------------------------------------------------------ Plaid Link callbacks

  const onLinkSuccess = useCallback(
    async (publicToken: string) => {
      setLinkToken(null);
      setPhase('exchange');
      try {
        const res = await api.plaid.exchange(publicToken, kind);
        setItem(res.item);
        setFound(res.accounts);
        setSelected(Object.fromEntries(res.accounts.map((a) => [a.plaid_account_id, true])));
        setPickMode('new');
        setStep('pick');
      } catch (e) {
        setStep('kind');
        if (e instanceof ApiError && e.status === 409) {
          const detail = errorMessage(e);
          setNotice({
            tone: 'info',
            title: 'Already linked',
            body: /manage accounts/i.test(detail) ? detail : `${detail} To add accounts you left out, use Manage accounts in Settings.`,
          });
        } else {
          setNotice({ tone: 'error', title: 'Linking failed', body: errorMessage(e) });
        }
      }
    },
    [kind],
  );

  const onLinkExit = useCallback((error: PlaidLinkError | null) => {
    setLinkToken(null);
    setStep('kind');
    if (error) setNotice({ tone: 'error', title: 'Plaid Link closed with an error', body: error.display_message || error.error_message || error.error_code });
  }, []);

  // ------------------------------------------------------------ pick step

  const toImport = found.filter((a) => selected[a.plaid_account_id] && !a.imported);
  const toRemove = pickMode === 'manage' ? found.filter((a) => a.imported && !selected[a.plaid_account_id]) : [];
  const keepIds = found.filter((a) => selected[a.plaid_account_id] || (pickMode !== 'manage' && a.imported)).map((a) => a.plaid_account_id);
  const instName = item?.institution_name ?? 'your institution';

  /** Leaving the pick step of a brand-new link: remove the pending Item so nothing stays billed at Plaid. */
  async function abandonNewItem(): Promise<void> {
    if (!item || pickMode !== 'new') return;
    const it = item;
    setBusy(true);
    try {
      await api.plaid.removeItem(it.id);
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t cancel the connection', body: `${errorMessage(e)} Remove ${it.institution_name ?? 'it'} in Settings.` });
    } finally {
      setBusy(false);
      setItem(null);
    }
  }

  async function doImport() {
    if (!item || keepIds.length === 0) return;
    setConfirmRemove(false);
    setBusy(true);
    try {
      const res = await api.plaid.importAccounts(item.id, keepIds);
      const inst = res.item.institution_name ?? instName;
      if (pickMode === 'manage') {
        const parts = [toImport.length ? `${plural(toImport.length, 'account')} added` : '', toRemove.length ? `${plural(toRemove.length, 'account')} removed` : ''].filter(Boolean);
        toast.push({ tone: 'success', title: `Updated ${inst}`, body: parts.join(' · ') || 'No changes.' });
      } else if (!res.result.ok) {
        toast.push({ tone: 'warn', title: `Linked ${inst}`, body: `${plural(toImport.length, 'account')} imported, but the first sync failed (${res.result.error_code ?? 'error'}). Try Sync again later.` });
      } else {
        toast.push({ tone: 'success', title: `Linked ${inst}`, body: `${plural(toImport.length, 'account')} imported. Transactions sync in the background.` });
      }
      setItem(null);
      onDone?.({ type: 'linked', item: res.item });
      onClose();
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t import accounts', body: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  }

  function submitPick() {
    if (toRemove.length) setConfirmRemove(true);
    else void doImport();
  }

  // ------------------------------------------------------------ manual step

  const manualCheck = checkManual(manual);

  async function submitManual() {
    const body = manualCheck.body;
    if (!body || busy) return;
    setBusy(true);
    setFormError(null);
    try {
      const created = await api.accountsV2.create(body);
      if (onAdded) onAdded(created);
      else toast.push({ tone: 'success', title: `${created.name} was added.`, body: `We’ll remind you to update it in ${remindDays(created.category)} days.` });
      onDone?.({ type: 'manual', account: created });
      onClose();
    } catch (e) {
      setFormError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  // ------------------------------------------------------------ close / back

  async function close() {
    if (busy) return;
    if (step === 'pick') await abandonNewItem();
    onClose();
  }

  async function back() {
    if (step === 'pick') await abandonNewItem();
    setNotice(null);
    // From the bank's accounts, back to the kind; from the kind or the manual form, to the first choice.
    setStep(step === 'pick' ? 'kind' : 'choose');
  }

  const kindLabel = KIND_OPTIONS.find((k) => k.kind === kind)?.label ?? 'Link an institution';
  const header: Record<Step, [string, string]> = {
    choose: ['Add an account', ''],
    kind: ['Connect a bank', 'What kind of account is it?'],
    connecting:
      phase === 'loading'
        ? [item?.institution_name ?? 'Accounts', 'Asking Plaid which accounts this login can see…']
        : [kindLabel, phase === 'exchange' ? 'Finding your accounts…' : 'Waiting for Plaid…'],
    pick:
      pickMode === 'manage'
        ? [`Manage ${instName}`, 'Choose which accounts Iron Owl keeps in sync.']
        : [instName, 'Choose which accounts to bring into Iron Owl.'],
    manual: ['Add an account yourself', ''],
  };
  const [title, subtitle] = header[step];
  const canBack = step === 'manual' || step === 'kind' || (step === 'pick' && pickMode === 'new');
  const linkActive = linkToken !== null;
  const phaseBusy = step === 'connecting';

  let footer = null;
  if (step === 'pick') {
    const n = pickMode === 'manage' ? keepIds.length : toImport.length;
    const changed = pickMode !== 'manage' || toImport.length > 0 || toRemove.length > 0;
    const label =
      pickMode === 'manage' ? (busy ? 'Saving…' : 'Save changes') : busy ? 'Importing…' : n ? `Import ${plural(n, 'account')}` : 'Import';
    footer = (
      <>
        {pickMode === 'manage' && toRemove.length > 0 && (
          <span className="small neg aa-foot-note">
            {plural(toRemove.length, 'account')} will be removed from Iron Owl
          </span>
        )}
        {keepIds.length === 0 && <span className="small subtle aa-foot-note">Choose at least one account.</span>}
        <button type="button" className="btn btn-ghost" onClick={() => void close()} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={submitPick} disabled={busy || keepIds.length === 0 || n === 0 || !changed}>
          {label}
        </button>
      </>
    );
  } else if (step === 'manual') {
    const ready = manualReady(manual);
    footer = (
      <>
        {!ready && <span className="small subtle aa-foot-note">Pick a kind, then type a name and a balance.</span>}
        <button type="button" className="aa-btn" onClick={() => void close()} disabled={busy}>
          Cancel
        </button>
        <button type="submit" form={formId} className="aa-btn aa-btn-primary" disabled={busy || !ready}>
          {busy ? 'Adding…' : 'Add account'}
        </button>
      </>
    );
  } else if (step === 'choose' || step === 'kind') {
    footer = (
      <button type="button" className="aa-btn" onClick={() => void close()} disabled={busy}>
        Cancel
      </button>
    );
  }

  return (
    <>
      <Modal
        className="aa-modal"
        open={open && !linkActive}
        title={title}
        subtitle={subtitle}
        onClose={() => void close()}
        onBack={canBack ? () => void back() : undefined}
        busy={busy || phaseBusy}
        footer={footer}
      >
        <div className="aa" ref={bodyRef}>
          {step === 'choose' && (
            <ChooseStep
              configured={plaidConfigured}
              notice={notice}
              onConnect={() => {
                setNotice(null);
                setStep('kind');
              }}
              onManual={() => {
                setNotice(null);
                setStep('manual');
              }}
              onClose={onClose}
            />
          )}

          {step === 'kind' && <KindStep configured={plaidConfigured} notice={notice} slots={slots} onPick={(k) => void beginLink(k)} onClose={onClose} />}

          {step === 'connecting' && (
            <div className="aa-connecting" role="status">
              <svg className="aa-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <circle cx="12" cy="12" r="9" strokeWidth="2.5" />
                <path d="M21 12a9 9 0 0 0-9-9" strokeWidth="2.5" strokeLinecap="round" />
              </svg>
              <div className="aa-connecting-title">
                {phase === 'exchange' ? 'Finding your accounts…' : phase === 'loading' ? 'Loading accounts…' : 'Connecting to Plaid…'}
              </div>
              <p>You sign in through Plaid’s window. Iron Owl never sees your bank password; it only gets read access to balances and transactions.</p>
            </div>
          )}

          {step === 'pick' && (
            <PickStep
              found={found}
              selected={selected}
              mode={pickMode}
              disabled={busy}
              onToggle={(id) => setSelected((s) => ({ ...s, [id]: !s[id] }))}
            />
          )}

          {step === 'manual' && (
            <AddManualForm
              formId={formId}
              draft={manual}
              onChange={(d) => {
                setManual(d);
                setFormError(null);
              }}
              onSubmit={() => void submitManual()}
              errors={manualCheck.errors}
              busy={busy}
              formError={formError}
            />
          )}
        </div>
      </Modal>

      {linkToken && <PlaidLinkLauncher key={linkToken} token={linkToken} onSuccess={(t) => void onLinkSuccess(t)} onExit={onLinkExit} />}

      <ConfirmDialog
        open={confirmRemove}
        title={`Remove ${plural(toRemove.length, 'account')} from Iron Owl?`}
        confirmLabel="Remove and save"
        busy={busy}
        onCancel={() => setConfirmRemove(false)}
        onConfirm={() => void doImport()}
      >
        <p>
          {toRemove.map((a) => a.name).join(', ')} will stop syncing, and {toRemove.length === 1 ? 'its' : 'their'} transactions, holdings and balance
          history will be deleted from Iron Owl. You can import {toRemove.length === 1 ? 'it' : 'them'} again later, but past history won’t come back.
        </p>
      </ConfirmDialog>
    </>
  );
}

function NoticeBox({ notice }: { notice: Notice | null }) {
  if (!notice) return null;
  return (
    <div className={`aa-notice${notice.tone === 'error' ? ' is-error' : ''}`} role={notice.tone === 'error' ? 'alert' : 'status'}>
      <Icon name={notice.tone === 'error' ? 'alert' : 'info'} />
      <div>
        <strong>{notice.title}</strong>
        <div>{notice.body}</div>
      </div>
    </div>
  );
}

function KeysNote({ onClose, tail }: { onClose: () => void; tail: string }) {
  return (
    <p className="aa-note">
      Connecting a bank needs the Plaid keys first.{' '}
      <Link to="/settings/banks?focus=bank-connection" onClick={onClose}>
        Set them up in Settings › Banks
      </Link>
      {tail}
    </p>
  );
}

/** Two big choices (design): Connect a bank (recommended) or Add one yourself. */
function ChooseStep({
  configured,
  notice,
  onConnect,
  onManual,
  onClose,
}: {
  configured: boolean | null;
  notice: Notice | null;
  onConnect: () => void;
  onManual: () => void;
  onClose: () => void;
}) {
  const off = configured === false;
  return (
    <div className="aa-choose aa-tiles">
      <NoticeBox notice={notice} />
      <button type="button" className="aa-tile is-rec" onClick={onConnect} disabled={off}>
        <span className="aa-tile-title">
          Connect a bank <span className="aa-tile-rec">· Recommended</span>
        </span>
        <span className="aa-tile-desc">Sign in to your bank through our secure partner. Balances and purchases update on their own.</span>
      </button>
      {off && <KeysNote onClose={onClose} tail=", or add the account yourself." />}
      <button type="button" className="aa-tile" onClick={onManual}>
        <span className="aa-tile-title">Add one yourself</span>
        <span className="aa-tile-desc">For accounts your bank can’t connect, like some loans. You type in the balance, and we remind you when it’s time to update it.</span>
      </button>
    </div>
  );
}

/** After "Connect a bank": which kind (Plaid's list of banks depends on it), and how many connections are used. */
function KindStep({
  configured,
  notice,
  slots,
  onPick,
  onClose,
}: {
  configured: boolean | null;
  notice: Notice | null;
  slots: BankSlots | null;
  onPick: (k: ItemKind) => void;
  onClose: () => void;
}) {
  const off = configured === false;
  const full = slotsFull(slots);
  return (
    <div className="aa-choose">
      <NoticeBox notice={notice} />
      {off && <KeysNote onClose={onClose} tail="." />}
      <div className="aa-options" role="group" aria-label="What kind of account">
        {KIND_OPTIONS.map((k) => (
          <button key={k.kind} type="button" className="aa-option" onClick={() => onPick(k.kind)} disabled={off || full || configured === null}>
            <span className="aa-chip" style={{ '--h': k.hue } as CSSProperties} aria-hidden="true">
              <Icon name={k.icon} />
            </span>
            <span className="aa-option-text">
              <span className="t">{k.label}</span>
              <span className="d">{k.desc}</span>
            </span>
            <Icon name="chevronRight" className="aa-chev" />
          </button>
        ))}
      </div>
      {slots && (
        <p className={`aa-slots${full ? ' is-full' : ''}`}>
          {full
            ? allUsedText(slots.limit)
            : `You’ve used ${slots.used} of ${slots.limit} bank connections. Each bank you connect uses one, even if you remove it later.`}
        </p>
      )}
    </div>
  );
}

function PickStep({
  found,
  selected,
  mode,
  disabled,
  onToggle,
}: {
  found: DiscoveredAccount[];
  selected: Record<string, boolean>;
  mode: PickMode;
  disabled: boolean;
  onToggle: (id: string) => void;
}) {
  if (found.length === 0) {
    return <p className="aa-note" style={{ padding: '8px 8px 4px' }}>Plaid didn’t return any accounts for this login. Cancel and try another institution, or add the account manually.</p>;
  }
  return (
    <fieldset className="aa-pick">
      <legend className="sr-only">Accounts to import</legend>
      {found.map((a) => {
        const locked = a.imported && mode !== 'manage';
        const on = locked || !!selected[a.plaid_account_id];
        const sub = [a.mask ? `••${a.mask}` : null, TYPE_NAME[a.category], locked ? 'Already imported' : null].filter(Boolean).join(' · ');
        return (
          <label key={a.plaid_account_id} className={`aa-pick-row${locked ? ' is-locked' : ''}`}>
            <input type="checkbox" className="aa-check" checked={on} disabled={locked || disabled} onChange={() => onToggle(a.plaid_account_id)} />
            <span className="aa-pick-text">
              <span className="t truncate">{a.name}</span>
              <span className="d truncate">{sub}</span>
            </span>
            <span className={`aa-pick-amt num${a.is_liability ? ' liab' : ''}`}>{formatMoney(a.is_liability ? -a.current_balance : a.current_balance)}</span>
          </label>
        );
      })}
    </fieldset>
  );
}
