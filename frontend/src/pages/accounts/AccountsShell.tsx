import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Outlet, useLocation, useNavigate, useOutletContext } from 'react-router-dom';
import { api, ApiError, errorMessage, type Account } from '../../api';
import { useApp, useUpdated } from '../../state';
import { useApi } from '../../lib/useApi';
import { plural } from '../../lib/format';
import { useToast } from '../../components/Toast';
import { UndoToast, type UndoMessage } from '../../components/UndoToast';
import { AddAccountModal, type AddAccountStart } from '../../components/AddAccountModal';
import { remindDays } from '../../components/manualAccount';
import './accounts.css';

/** A message at the bottom (the shared 12 s UndoToast). `undo` runs once; a failure becomes an error toast. */
export type Say = (title: ReactNode, body?: ReactNode, undo?: () => Promise<unknown>) => void;

/** What the list and the detail page share (react-router outlet context). */
export interface AccountsCtx {
  accounts: Account[] | undefined;
  error: unknown;
  reload: () => void;
  /** Put a changed account in place (then `invalidate()` so Home and totals follow). */
  replace: (a: Account) => void;
  drop: (id: number) => void;
  add: (a: Account) => void;
  say: Say;
  openAdd: (start: AddAccountStart) => void;
  /** Grouped-loans rows the user opened (kept while they visit a loan and comes back). */
  openGroups: Set<string>;
  toggleGroup: (key: string) => void;
  hiddenOpen: boolean;
  setHiddenOpen: (open: boolean) => void;
}

export const useAccounts = () => useOutletContext<AccountsCtx>();

/**
 * Accounts (design `Accounts Beginner.dc.html`, layout A): the header ("{N} accounts · Updated …",
 * Update from banks, + Add account), the account list (index) or one account (`/accounts/:id`)
 * in its place, and one Undo message at a time. The old `#acct-N` links open that account.
 */
export function AccountsShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const toast = useToast();
  const { invalidate, sync, syncing, plaid } = useApp();
  const updated = useUpdated();
  const { data, error, reload, setData } = useApi(() => api.accounts.list(), []);
  const [addStart, setAddStart] = useState<AddAccountStart | null>(null);
  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set());
  const [hiddenOpen, setHiddenOpen] = useState(false);

  // ---------------------------------------------------------------- messages
  const [msg, setMsg] = useState<(UndoMessage & { run?: () => Promise<unknown> }) | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);
  const msgKey = useRef(0);
  const say = useCallback<Say>((title, body, undo) => {
    msgKey.current += 1;
    setUndoBusy(false);
    setMsg({ key: msgKey.current, title, body, undo: !!undo, run: undo });
  }, []);
  async function runUndo() {
    const m = msg;
    if (!m?.run || undoBusy) return;
    setUndoBusy(true);
    try {
      await m.run();
      setMsg(null);
    } catch (e) {
      setMsg(null);
      if (!(e instanceof ApiError && e.status === 401)) toast.push({ tone: 'error', title: 'Couldn’t undo that', body: errorMessage(e) });
    } finally {
      setUndoBusy(false);
    }
  }

  // ---------------------------------------------------------------- links from elsewhere
  // ?new=1 opens Add account on the manual form (links from other pages); ?add=1 on the choice (command palette).
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    if (q.get('new') === '1') setAddStart({ step: 'manual' });
    else if (q.get('add') === '1') setAddStart({ step: 'choose' });
  }, [location.search]);
  // The old "#acct-12" links (bookmarks, older builds) open that account's page.
  useEffect(() => {
    const m = /^#acct-(\d{1,9})$/.exec(location.hash);
    if (m) navigate(`/accounts/${m[1]}`, { replace: true });
  }, [location.hash, navigate]);

  const closeAdd = () => {
    setAddStart(null);
    if (location.search) navigate({ pathname: location.pathname, search: '' }, { replace: true });
  };

  // ---------------------------------------------------------------- list edits
  const replace = useCallback((a: Account) => setData((prev) => (prev ?? []).map((x) => (x.id === a.id ? a : x))), [setData]);
  const drop = useCallback((id: number) => setData((prev) => (prev ?? []).filter((x) => x.id !== id)), [setData]);
  const add = useCallback((a: Account) => setData((prev) => [...(prev ?? []).filter((x) => x.id !== a.id), a]), [setData]);
  const toggleGroup = useCallback(
    (key: string) =>
      setOpenGroups((prev) => {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      }),
    [],
  );

  const ctx = useMemo<AccountsCtx>(
    () => ({
      accounts: data,
      error,
      reload,
      replace,
      drop,
      add,
      say,
      openAdd: setAddStart,
      openGroups,
      toggleGroup,
      hiddenOpen,
      setHiddenOpen,
    }),
    [data, error, reload, replace, drop, add, say, openGroups, toggleGroup, hiddenOpen],
  );

  // ---------------------------------------------------------------- header
  const visible = data?.filter((a) => !a.hidden).length ?? 0;
  const connected = !!data?.some((a) => a.connection);
  const canUpdate = connected && plaid?.configured !== false;

  async function updateFromBanks() {
    if (syncing) return;
    const results = await sync({ quiet: 'all' });
    if (!results) return; // already running, or it failed (state.tsx says so)
    if (results.length === 0) {
      toast.push({ tone: 'info', title: 'There’s no bank to update from yet.' });
      return;
    }
    const failed = results.filter((r) => !r.ok);
    if (failed.length === 0) {
      say('Updated from your banks just now.');
      return;
    }
    toast.push({
      tone: 'warn',
      title: failed.length === 1 ? `${failed[0]!.institution_name ?? 'One bank'} couldn’t be updated` : `${failed.length} banks couldn’t be updated`,
      body: 'Its accounts show what to do next.',
    });
  }

  return (
    <div className="accounts">
      <header className="ac-head">
        <div>
          <h1 className="ac-title">Accounts</h1>
          <p className="ac-sub">
            {data ? plural(visible, 'account') : ' '}
            {data && connected && ` · ${syncing ? 'Updating from your banks…' : updated.text}`}
          </p>
        </div>
        <div className="ac-head-actions">
          {canUpdate && (
            <button type="button" className="ac-btn" onClick={() => void updateFromBanks()} aria-disabled={syncing || undefined}>
              {syncing ? 'Updating…' : 'Update from banks'}
            </button>
          )}
          <button type="button" className="ac-btn ac-btn-primary" onClick={() => setAddStart({ step: 'choose' })}>
            <span aria-hidden="true">+ </span>Add account
          </button>
        </div>
      </header>

      <Outlet context={ctx} />

      <AddAccountModal
        open={addStart !== null}
        start={addStart ?? undefined}
        onClose={closeAdd}
        onAdded={(a) => {
          add(a);
          invalidate();
          // Show it in its group (from an account's page too).
          if (location.pathname !== '/accounts') navigate('/accounts');
          say(`${a.name} was added.`, `We’ll remind you to update it in ${remindDays(a.category)} days.`, async () => {
            await api.accountsV2.remove(a.id);
            drop(a.id);
            invalidate();
            // The user may have opened it meanwhile: its page would say it's gone, so go back to the list.
            if (window.location.hash.split('?')[0] === `#/accounts/${a.id}`) navigate('/accounts');
          });
        }}
        onDone={(r) => {
          if (r.type === 'linked') {
            reload();
            invalidate();
          }
        }}
      />

      <UndoToast message={msg} onUndo={msg?.run ? () => void runUndo() : undefined} undoBusy={undoBusy} onClose={() => setMsg(null)} />
    </div>
  );
}
