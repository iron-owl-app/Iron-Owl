import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import type { PlaidLinkError } from 'react-plaid-link';
import { api, ApiError, errorMessage, type ItemKind } from '../api';
import { useApp } from '../state';
import { useToast } from './Toast';
import { PlaidLinkLauncher } from './PlaidLinkLauncher';

/** The bits of a Plaid connection the sign-in needs (a PlaidItem or a Home alert's data). */
export interface ReauthTarget {
  id: number;
  kind: ItemKind;
  institution_name: string | null;
}

export type ReauthPhase = 'opening' | 'updating';

/**
 * "Sign in again" to a bank: opens Plaid Link in update mode for an existing connection,
 * then syncs it. Used by Home's "Sign in to {Bank}" alert and Settings → Linked institutions.
 *
 *   const reauth = useBankReauth();
 *   <button onClick={() => reauth.start(item)} aria-disabled={reauth.busyItemId === item.id}>…</button>
 *   {reauth.launcher}
 *
 * Plaid Link draws its own full-screen window; callers must not keep a modal <dialog> open
 * while `launcher` is mounted. Messages: success → "{Bank} is connected again. Balances are
 * up to date." (6s); still failing → a persistent error; closed without an error → nothing.
 */
export function useBankReauth(opts: { onDone?: (ok: boolean, item: ReauthTarget) => void } = {}): {
  start: (item: ReauthTarget) => void;
  busyItemId: number | null;
  phase: ReauthPhase | null;
  launcher: ReactNode;
} {
  const toast = useToast();
  const navigate = useNavigate();
  const { sync } = useApp();
  const [busy, setBusy] = useState<{ item: ReauthTarget; phase: ReauthPhase } | null>(null);
  const [session, setSession] = useState<{ token: string; item: ReauthTarget } | null>(null);
  const onDoneRef = useRef(opts.onDone);
  onDoneRef.current = opts.onDone;
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const start = useCallback(
    (item: ReauthTarget) => {
      if (busy) return;
      setBusy({ item, phase: 'opening' });
      api.plaid
        .linkToken(item.kind, item.id)
        .then(({ link_token }) => {
          if (live.current) setSession({ token: link_token, item });
        })
        .catch((e: unknown) => {
          if (!live.current) return;
          setBusy(null);
          if (e instanceof ApiError && e.status === 401) return;
          if (e instanceof ApiError && e.status === 503) {
            toast.push({ tone: 'info', title: 'Your Plaid keys aren’t set', body: 'Add them under Bank connection, then sign in to your bank again.' });
            navigate('/settings/banks?focus=bank-connection');
            return;
          }
          toast.push({ tone: 'error', title: 'Couldn’t open the bank sign-in', body: errorMessage(e) });
        });
    },
    [busy, navigate, toast],
  );

  const onSuccess = useCallback(async () => {
    const s = session;
    setSession(null);
    if (!s) return;
    const bank = s.item.institution_name ?? 'Your bank';
    setBusy({ item: s.item, phase: 'updating' });
    const results = await sync({ itemId: s.item.id, quiet: 'all' });
    if (!live.current) return;
    setBusy(null);
    if (!results) return onDoneRef.current?.(false, s.item); // the request failed; sync already said so
    const ok = results.length > 0 && results.every((r) => r.ok);
    if (ok) {
      toast.push({ tone: 'success', title: `${bank} is connected again. Balances are up to date.`, timeout: 6000 });
    } else {
      toast.push({ tone: 'error', title: `${bank} still needs you to sign in. Try again.` });
    }
    onDoneRef.current?.(ok, s.item);
  }, [session, sync, toast]);

  const onExit = useCallback(
    (error: PlaidLinkError | null) => {
      const s = session;
      setSession(null);
      setBusy(null);
      if (error) {
        toast.push({
          tone: 'error',
          title: `Couldn’t finish signing in to ${s?.item.institution_name ?? 'your bank'}`,
          body: error.display_message || error.error_message || 'Plaid closed with an error. Try again in a few minutes.',
        });
      }
    },
    [session, toast],
  );

  return {
    start,
    busyItemId: busy?.item.id ?? null,
    phase: busy?.phase ?? null,
    launcher: session ? <PlaidLinkLauncher key={session.token} token={session.token} onSuccess={() => void onSuccess()} onExit={onExit} /> : null,
  };
}
