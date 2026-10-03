import { useEffect, useId, useState, type CSSProperties } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import { api, ApiError, errorMessage, type AutoSync, type ItemKind, type PlaidItem } from '../api';
import { useApp } from '../state';
import { useApi } from '../lib/useApi';
import { formatDateTime, formatRelative, plural } from '../lib/format';
import { Icon, type IconName } from '../components/Icon';
import { AddAccountModal, KIND_OPTIONS, type AddAccountStart } from '../components/AddAccountModal';
import { Avatar, Skeleton, SkeletonRows } from '../components/ui';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { useBankReauth } from '../components/useBankReauth';
import { useToast } from '../components/Toast';
import { allUsedText, checkUsedCount, slotsFull, slotsOf, usedRowText, type BankSlots } from '../components/bankSlots';
import { BankConnectionSection } from './settings/BankConnectionSection';
import { DialogActions, StDialog, StSwitch } from './settings/parts';
import './settings/settings.css';

/**
 * Settings › Banks › Manage banks (/settings/banks, design D7): everything about bank
 * connections the Settings tabs have no room for. Plaid keys (Bank connection), the linked
 * institutions with add / sign in again / manage accounts / sync / remove, and the automatic
 * sync schedule. Deep links: `?focus=bank-connection` (the keys card), `?focus=connections`,
 * and `?finish={item id}` (opens "Finish setting up" for a bank whose accounts aren't chosen).
 */
export function SettingsBanksPage() {
  const [params] = useSearchParams();
  const location = useLocation();
  const focus = params.get('focus');

  // Deferred to a task: Layout moves focus to <main> (and scrolls to top) in its own effect after ours.
  useEffect(() => {
    if (focus !== 'bank-connection' && focus !== 'connections') return;
    const h = window.setTimeout(() => {
      const [cardId, headId] = focus === 'bank-connection' ? ['bank-connection', 'bank-h'] : ['connections', 'conn-h'];
      const card = document.getElementById(cardId);
      const top = card?.getBoundingClientRect().top ?? 0;
      if (card && (top < 0 || top > window.innerHeight * 0.6)) card.scrollIntoView({ block: 'start' });
      document.getElementById(headId)?.focus({ preventScroll: true });
    });
    return () => window.clearTimeout(h);
  }, [focus, location.key]);

  return (
    <div className="st-page st-page-wide">
      <header className="st-head">
        <Link to="/settings?tab=banks" className="st-back">
          <Icon name="chevronLeft" />
          Settings
        </Link>
        <h1 className="st-title">Manage banks</h1>
        <p className="st-desc">Your Plaid keys, the banks Iron Owl connects to, and how often it checks them.</p>
      </header>
      <div className="settings-grid">
        <BankConnectionSection />
        <ConnectionsSection />
        <AutoSyncSection />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- linked institutions

const KIND_META: Record<ItemKind, { label: string; desc: string; icon: IconName; noun: string }> = {
  bank: { label: 'Add bank or card', desc: 'Checking, savings, credit cards', icon: 'bank', noun: 'Bank' },
  investment: { label: 'Add 401(k) · HSA · investment', desc: 'Retirement, HSA, brokerage holdings', icon: 'trend', noun: 'Investments' },
  loan: { label: 'Add loan', desc: 'Mortgage, student loan, card APRs', icon: 'house', noun: 'Loans' },
};

function ConnectionsSection() {
  const toast = useToast();
  const { invalidate, sync, syncing } = useApp();
  const [params, setParams] = useSearchParams();
  const status = useApi(() => api.plaid.status(), []);
  const items = useApi(() => api.plaid.items(), []);
  const reauth = useBankReauth({ onDone: () => items.reload() });
  const [addStart, setAddStart] = useState<AddAccountStart | null>(null);
  const [removeTarget, setRemoveTarget] = useState<PlaidItem | null>(null);
  const [removing, setRemoving] = useState(false);
  const [syncingItem, setSyncingItem] = useState<number | null>(null);
  const [changeOpen, setChangeOpen] = useState(false);
  const [capSaving, setCapSaving] = useState(false);

  // ?finish={id}: "Finish setting up" from Home or the Settings Needs-you card.
  const finishId = Number(params.get('finish') ?? NaN);
  useEffect(() => {
    if (!Number.isFinite(finishId) || !items.data || !status.data) return;
    const it = items.data.find((i) => i.id === finishId);
    setParams(
      (p) => {
        const next = new URLSearchParams(p);
        next.delete('finish');
        return next;
      },
      { replace: true },
    );
    if (it && status.data.configured) setAddStart({ step: 'manage', item: it });
  }, [finishId, items.data, status.data, setParams]);

  async function syncOne(item: PlaidItem) {
    setSyncingItem(item.id);
    await sync({ itemId: item.id });
    setSyncingItem(null);
    items.reload();
  }

  async function doRemove() {
    if (!removeTarget) return;
    setRemoving(true);
    try {
      await api.plaid.removeItem(removeTarget.id);
      toast.push({ tone: 'success', title: `Removed ${removeTarget.institution_name ?? 'connection'}` });
      setRemoveTarget(null);
      items.setData((prev) => (prev ?? []).filter((i) => i.id !== removeTarget.id));
      status.reload();
      invalidate();
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t remove connection', body: errorMessage(e) });
    } finally {
      setRemoving(false);
    }
  }

  // Iron Owl 2.0.0: counting against Plaid's Trial limit is a switch (off: no count, nothing blocked).
  async function setCap(on: boolean) {
    if (capSaving) return;
    setCapSaving(true);
    try {
      const next = await api.connectionsUsed.setCap(on);
      if (status.data) status.setData({ ...status.data, ...next });
      toast.push({ tone: 'success', title: on ? 'Counting bank connections' : 'Bank connections aren’t counted', timeout: 3000 });
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) toast.push({ tone: 'error', title: 'Couldn’t change this', body: errorMessage(e) });
    } finally {
      setCapSaving(false);
    }
  }

  const configured = status.data?.configured;
  const busy = reauth.busyItemId !== null;
  // Plaid Trial: 10 bank connections, ever. When all are used, say so instead of offering to link.
  const slots = slotsOf(status.data);
  const full = slotsFull(slots);

  return (
    <section className="panel" id="connections" aria-labelledby="conn-h">
      <div className="panel-head">
        <div>
          <h2 id="conn-h" tabIndex={-1}>
            Linked institutions
          </h2>
          <p className="small muted">Bank, retirement, HSA and loan connections through Plaid.</p>
        </div>
        {status.data && (
          <span className={`badge ${configured ? (status.data.env === 'production' ? 'badge-ok' : 'badge-accent') : ''}`}>
            {configured ? (status.data.env === 'production' ? 'Real banks' : 'Test banks') : 'Plaid keys not set'}
          </span>
        )}
      </div>

      <div className="panel-body">
        {status.loading && !status.data ? (
          <div className="link-buttons" aria-hidden="true">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} height={58} style={{ borderRadius: 10 }} />
            ))}
          </div>
        ) : status.error ? (
          <p className="field-error">{errorMessage(status.error)}</p>
        ) : configured && full && slots ? (
          <p className="st-note st-note-top bk-full" role="status">
            {allUsedText(slots.limit)}
          </p>
        ) : configured ? (
          <div className="link-buttons">
            {KIND_OPTIONS.map((k) => (
              <button key={k.kind} type="button" className="link-card" onClick={() => setAddStart({ step: 'link', kind: k.kind })} disabled={busy}>
                <span className="cat-chip" style={{ '--h': k.hue } as CSSProperties}>
                  <Icon name={k.icon} />
                </span>
                <span>
                  <span className="t">{KIND_META[k.kind].label}</span>
                  <span className="d">{KIND_META[k.kind].desc}</span>
                </span>
              </button>
            ))}
          </div>
        ) : (
          <p className="muted">
            Add your Plaid keys under <Link to="/settings/banks?focus=bank-connection">Bank connection</Link> above to link banks. Until then,{' '}
            <Link to="/accounts?new=1">manual accounts</Link> work fully: balances, history, loans and net worth.
          </p>
        )}
        {slots && (
          <div className="bk-used">
            <span>{usedRowText(slots)}</span>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setChangeOpen(true)} aria-label="Change the number of bank connections used">
              Change…
            </button>
          </div>
        )}
        {status.data && (
          <div className="bk-cap">
            <p className="bk-cap-text">
              <span className="bk-cap-title">Count bank connections</span>{' '}
              <span className="bk-cap-note">(Plaid’s free Trial plan allows 10, ever)</span>
            </p>
            <StSwitch
              checked={status.data.items_cap === true}
              onChange={(on) => void setCap(on)}
              label="Count bank connections (Plaid’s free Trial plan allows 10, ever)"
              disabled={capSaving}
            />
          </div>
        )}
      </div>

      {items.loading && !items.data ? (
        <SkeletonRows rows={2} label="Loading connections" />
      ) : items.data && items.data.length > 0 ? (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }} aria-label="Connections">
          {items.data.map((it) => {
            const name = it.institution_name ?? 'connection';
            const pending = it.status === 'pending';
            return (
              <li className="item-row" key={it.id}>
                <div className="row-main">
                  <Avatar name={it.institution_name ?? 'Institution'} />
                  <div style={{ minWidth: 0 }}>
                    <div className="row-title">
                      <span className="truncate">{it.institution_name ?? 'Unknown institution'}</span>
                      <StatusBadge item={it} />
                    </div>
                    <div className="row-sub">
                      {pending
                        ? `${KIND_META[it.kind].noun} · accounts not chosen yet`
                        : `${KIND_META[it.kind].noun} · ${plural(it.account_count, 'account')} · synced ${formatRelative(it.last_synced_at)}`}
                    </div>
                  </div>
                </div>
                <div className="item-actions">
                  {pending ? (
                    <button
                      type="button"
                      className="btn btn-sm btn-primary"
                      onClick={() => setAddStart({ step: 'manage', item: it })}
                      disabled={!configured || busy}
                      aria-label={`Finish setting up ${name}`}
                    >
                      <Icon name="check" />
                      Finish setup
                    </button>
                  ) : (
                    <>
                      <button
                        type="button"
                        className={`btn btn-sm ${it.status === 'login_required' ? 'btn-primary' : 'btn-ghost'}`}
                        onClick={() => reauth.start(it)}
                        disabled={!configured || (busy && reauth.busyItemId !== it.id)}
                        aria-disabled={reauth.busyItemId === it.id || undefined}
                        aria-label={reauth.busyItemId === it.id ? undefined : `Sign in again to ${name}`}
                      >
                        {reauth.busyItemId === it.id ? <Icon name="sync" className="spin" /> : <Icon name="key" />}
                        {reauth.busyItemId === it.id ? (reauth.phase === 'updating' ? 'Updating…' : 'Opening sign-in…') : 'Sign in again'}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        onClick={() => setAddStart({ step: 'manage', item: it })}
                        disabled={!configured || busy}
                        aria-label={`Manage accounts for ${name}`}
                      >
                        <Icon name="sliders" />
                        Manage accounts
                      </button>
                      <button type="button" className="btn btn-sm btn-ghost" onClick={() => void syncOne(it)} disabled={!configured || syncing} aria-label={`Sync ${name}`}>
                        <Icon name="sync" className={syncingItem === it.id ? 'spin' : undefined} />
                        Sync
                      </button>
                    </>
                  )}
                  <button type="button" className="btn btn-sm btn-ghost btn-danger-quiet" onClick={() => setRemoveTarget(it)} aria-label={`Remove ${name}`}>
                    <Icon name="trash" />
                    Remove
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : configured ? (
        <p className="small subtle" style={{ padding: '0 16px 16px' }}>
          No institutions linked yet. You can also <Link to="/accounts?new=1">add accounts manually</Link>.
        </p>
      ) : null}

      {reauth.launcher}

      <AddAccountModal
        open={addStart !== null}
        start={addStart ?? undefined}
        onClose={() => setAddStart(null)}
        onDone={() => {
          items.reload();
          status.reload();
          invalidate();
        }}
      />

      {slots && (
        <UsedCountDialog
          open={changeOpen}
          slots={slots}
          onClose={() => setChangeOpen(false)}
          onSaved={(next) => {
            if (status.data) status.setData({ ...status.data, ...next });
            setChangeOpen(false);
            toast.push({ tone: 'success', title: usedRowText({ used: next.items_linked, limit: next.items_limit, now: next.items_now }), timeout: 3000 });
          }}
        />
      )}

      <ConfirmDialog
        open={removeTarget !== null}
        title={`Remove ${removeTarget?.institution_name ?? 'this connection'}?`}
        confirmLabel="Remove connection"
        busy={removing}
        onCancel={() => setRemoveTarget(null)}
        onConfirm={() => void doRemove()}
      >
        <p>
          {removeTarget?.status === 'pending'
            ? 'This disconnects the unfinished connection at Plaid. No accounts were imported from it yet.'
            : `This disconnects it at Plaid and deletes its ${plural(removeTarget?.account_count ?? 0, 'account')}, their transactions, holdings and balance history from Iron Owl. It can’t be undone, and past history won’t come back.`}
        </p>
        {slots && (
          <p>
            <strong>You can’t get this connection back without using one of your limited bank connections.</strong>
          </p>
        )}
      </ConfirmDialog>
    </section>
  );
}

/**
 * "Change…": the Plaid dashboard knows the real number (connections removed before Release 3.10
 * weren't counted here). A whole number from the connections there are now up to the limit.
 */
function UsedCountDialog({
  open,
  slots,
  onClose,
  onSaved,
}: {
  open: boolean;
  slots: BankSlots;
  onClose: () => void;
  onSaved: (next: { items_linked: number; items_limit: number; items_now: number }) => void;
}) {
  const uid = useId();
  const [draft, setDraft] = useState(String(slots.used));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setDraft(String(slots.used));
    setError(null);
    // Only when it opens: a refreshed count mustn't wipe what the user is typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  async function save() {
    const check = checkUsedCount(draft, slots);
    if (check.value === null) {
      setError(check.error);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSaved(await api.connectionsUsed.set(check.value));
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <StDialog open={open} title="Bank connections used" onClose={onClose} onSubmit={() => void save()} busy={busy} size={480}>
      <p className="st-help">If you connected banks before, the Plaid dashboard shows the real number. Type it here.</p>
      <div className="st-field">
        <label className="st-field-label" htmlFor={`${uid}-n`}>
          Connections used (from {slots.now} to {slots.limit})
        </label>
        <input
          id={`${uid}-n`}
          className="st-input bk-used-input"
          inputMode="numeric"
          autoComplete="off"
          maxLength={2}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value.replace(/\D/g, '').slice(0, 2));
            setError(null);
          }}
          aria-invalid={!!error || undefined}
          aria-describedby={error ? `${uid}-err` : undefined}
          disabled={busy}
          autoFocus
        />
        {error && (
          <p className="bk-used-error" id={`${uid}-err`} role="alert">
            {error}
          </p>
        )}
      </div>
      <DialogActions onCancel={onClose} submitLabel={busy ? 'Saving…' : 'Save'} busy={busy} />
    </StDialog>
  );
}

function StatusBadge({ item }: { item: PlaidItem }) {
  if (item.status === 'ok')
    return (
      <span className="badge badge-ok">
        <Icon name="check" />
        Connected
      </span>
    );
  if (item.status === 'pending')
    return (
      <span className="badge badge-warn">
        <Icon name="alert" />
        Setup not finished
      </span>
    );
  if (item.status === 'login_required')
    return (
      <span className="badge badge-warn">
        <Icon name="alert" />
        Needs you to sign in again
      </span>
    );
  return (
    <span className="badge badge-error" title={item.error_code ?? undefined}>
      <Icon name="alert" />
      {item.error_code ?? 'Error'}
    </span>
  );
}

// ---------------------------------------------------------------- automatic sync (Release 3)

const SYNC_CHOICES: { hours: AutoSync['hours']; label: string }[] = [
  { hours: 0, label: 'Off' },
  { hours: 3, label: '3h' },
  { hours: 6, label: '6h' },
  { hours: 12, label: '12h' },
  { hours: 24, label: '24h' },
];

function AutoSyncSection() {
  const toast = useToast();
  const auto = useApi(() => api.autoSync.get(), []);
  const status = useApi(() => api.plaid.status(), []);
  const [saving, setSaving] = useState<AutoSync['hours'] | null>(null);
  const hours = auto.data?.hours;

  async function choose(h: AutoSync['hours']) {
    if (h === hours || saving !== null) return;
    setSaving(h);
    try {
      const next = await api.autoSync.set(h);
      auto.setData(next);
      toast.push({ tone: 'success', title: h === 0 ? 'Automatic sync is off' : `Syncing every ${h} hours`, timeout: 3000 });
    } catch (err) {
      toast.push({ tone: 'error', title: 'Couldn’t change automatic sync', body: errorMessage(err) });
    } finally {
      setSaving(null);
    }
  }

  let note: string;
  if (!auto.data) note = ' ';
  else if (auto.data.hours === 0) note = 'Off. Use Sync in the sidebar whenever you want fresh balances.';
  else if (status.data && !status.data.configured) note = 'Add your Plaid keys under Bank connection to start syncing.';
  else if (auto.data.next_at) note = `Next sync ${formatRelative(auto.data.next_at)} (${formatDateTime(auto.data.next_at)}).`;
  else note = 'Starts once you link an institution.';

  return (
    <section className="panel" aria-labelledby="as-h">
      <div className="panel-head as-head">
        <div>
          <h2 id="as-h">Automatic sync</h2>
          <p className="small muted">While Iron Owl is unlocked, it syncs your linked institutions on this schedule. New deposits show up as money to assign.</p>
        </div>
      </div>
      <div className="panel-body as-body">
        {auto.error && !auto.data ? (
          <p className="field-error">{errorMessage(auto.error)}</p>
        ) : (
          <>
            <div className="segmented as-seg" role="group" aria-label="Sync every">
              {SYNC_CHOICES.map((c) => (
                <button
                  key={c.hours}
                  type="button"
                  aria-pressed={hours === c.hours}
                  aria-label={c.hours === 0 ? 'Off' : `Every ${c.hours} hours`}
                  onClick={() => void choose(c.hours)}
                  disabled={!auto.data || saving !== null}
                >
                  {saving === c.hours ? <Icon name="sync" className="spin" /> : c.hours === 0 ? 'Off' : `Every ${c.label}`}
                </button>
              ))}
            </div>
            <p className="small muted as-note" aria-live="polite">
              {note}
            </p>
          </>
        )}
      </div>
    </section>
  );
}
