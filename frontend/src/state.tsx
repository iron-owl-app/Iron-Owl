import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  api,
  ApiError,
  errorMessage,
  onConnectionLost,
  onLocked,
  setSessionToken,
  type AuthStatus,
  type PlaidStatus,
  type Presence,
  type SessionEnd,
  type SyncItemResult,
} from './api';
import { useToast } from './components/Toast';
import { reasonFromServer, type ElsewhereKind, type LockReason, type UnreachableCode } from './components/gate/gateCopy';
import { formatUpdated } from './lib/format';
import { readPref, writePref } from './lib/prefs';
import { getTabId } from './lib/tab';
import { clearHandoffNonce, ensureOwnTabId, isHandoffToken, listenTabs, pendingHandoffNonce, postTabs } from './lib/tabChannel';
import { useIdleTimer } from './lib/useIdleTimer';
import { usePresence } from './lib/usePresence';

export type Phase = 'loading' | 'unreachable' | 'setup' | 'locked' | 'elsewhere' | 'unlocked';
export type { ElsewhereKind, LockReason };
/** "FinTrack isn't running": which code the Details line reads out, and when it happened. */
export interface Unreachable {
  code: UnreachableCode;
  at: Date;
}

/** The first status read at start gives up after this long (then "FinTrack isn't running"). */
const FIRST_STATUS_TIMEOUT_MS = 20_000;
const SUPPORT_PREF = 'supportContact';

interface AppState {
  phase: Phase;
  autoLockMinutes: number;
  /** Settings › "Lock FinTrack when I step away for": the idle timer follows at once. */
  setAutoLockMinutes: (minutes: number) => void;
  /** Why the password screen is showing: Sign in (`start`), locked (idle/closed/manual/ended), or Open here. */
  lockReason: LockReason;
  /** "Already open": a second window (`second`) or this one lost the session to another (`moved`). */
  elsewhere: ElsewhereKind;
  /** Set in phase `unreachable`. */
  unreachable: Unreachable | null;
  /** Ask for the password to open FinTrack in this window (Open here with no answer from the other one). */
  showPasswordHere: () => void;
  /** Adopt a session handed over by the other window (Open here), then open FinTrack. */
  adoptSession: (token: string) => void;
  plaid: PlaidStatus | null;
  /** Re-read Plaid status (after Plaid keys are saved or removed in Settings). */
  refreshPlaid: () => Promise<void>;
  /** Bumped whenever server data may have changed; useApi refetches on it. */
  dataVersion: number;
  invalidate: () => void;
  syncing: boolean;
  /**
   * Sync with the banks. `quiet: 'success'` skips the toast when every connection worked
   * (the caller shows its own wording); `quiet: 'all'` skips the result toast entirely.
   * A failed request always shows an error.
   */
  sync: (opts?: SyncOptions) => Promise<SyncItemResult[] | null>;
  lockNow: (reason?: 'manual' | 'idle') => Promise<void>;
  /** Called by Setup/Unlock after the server set the session cookie. Once per unlock (later calls do nothing). */
  enterUnlocked: () => void;
  /** Already unlocked, but the session was replaced (a backup restore): reload everything. */
  reloadUnlocked: () => void;
  retryStatus: () => void;
  /** Items needing attention, mirrored for nav badges. */
  attention: number;
  setAttention: (n: number) => void;
  /** Newest bank update (summary.last_synced_at); Layout's summary fetch keeps it current. Read it with `useUpdated()`. */
  setUpdatedAt: (iso: string | null) => void;
  /** From /api/auth/status (no session needed): support contact, recovery sheet, lockout. */
  authInfo: AuthInfo;
  /** Increments once per unlocked session (updates: check once per unlock). */
  sessionNo: number;
  /** While an update installs (or its result is on screen), the idle timer doesn't lock the window. */
  holdIdle: (held: boolean) => void;
  /** Show "FinTrack isn't running" with this code (an update restart that never came back). */
  showUnreachable: (code: UnreachableCode) => void;
  /** Re-read /api/auth/status without changing the phase (unlock and recovery screens). */
  refreshAuthInfo: () => Promise<AuthInfo>;
}

export interface AuthInfo {
  /** "Sam", or null when the install doesn't name anyone (see supportName). */
  supportContact: string | null;
  /** null until known (or from an older server that doesn't say). */
  recovery: { available: boolean; sheet: string | null } | null;
  /** Seconds left in a "too many tries" wait when the status was read. */
  retryAfter: number | null;
  /** Wrong tries left before a wait (5 when none yet); null from an older server. */
  triesLeft: number | null;
}

/**
 * The support contact is also kept in UI prefs: "FinTrack isn't running" shows it when FinTrack
 * itself can't be asked. An empty string means "no one named".
 */
function cachedSupportContact(): string | null {
  const v = readPref<string>(SUPPORT_PREF, '');
  return v.trim() ? v.trim() : null;
}

function authInfoFrom(s: AuthStatus): AuthInfo {
  const contact = typeof s.support_contact === 'string' && s.support_contact.trim() ? s.support_contact.trim() : null;
  writePref(SUPPORT_PREF, contact ?? '');
  const rec = s.recovery && typeof s.recovery === 'object' ? { available: !!s.recovery.available, sheet: s.recovery.sheet ?? null } : null;
  const wait = typeof s.retry_after === 'number' && s.retry_after > 0 ? Math.ceil(s.retry_after) : null;
  const tries = typeof s.tries_left === 'number' && Number.isFinite(s.tries_left) ? Math.max(0, Math.floor(s.tries_left)) : null;
  return { supportContact: contact, recovery: rec, retryAfter: wait, triesLeft: tries };
}

/** Who to call: the configured support contact, else a generic phrase that fits mid-sentence. */
export function supportName(info: AuthInfo): string {
  return info.supportContact ?? 'the person who set up Iron Owl';
}

export interface SyncOptions {
  itemId?: number;
  auto?: boolean;
  quiet?: 'success' | 'all';
}

const Ctx = createContext<AppState | null>(null);

/** The shared "Updated …" time and a clock that ticks every minute (its own context, so the tick re-renders only its readers). */
interface Updated {
  /** ISO time of the newest bank update, or null (never, or not known yet). */
  updatedAt: string | null;
  /** "Updated 5 minutes ago" / "Not updated yet" (lib/format.formatUpdated), re-worked every minute. */
  text: string;
  /** The minute clock the text was worked out at. */
  now: Date;
}
const UpdatedCtx = createContext<Updated>({ updatedAt: null, text: formatUpdated(null), now: new Date() });

/** Sidebar, Accounts, Home and Reports headers: the same "Updated …" words everywhere. */
export function useUpdated(): Updated {
  return useContext(UpdatedCtx);
}

export function useApp(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useApp outside AppProvider');
  return ctx;
}

/**
 * Where "Link a bank" buttons should go (Settings › Banks › Manage banks, /settings/banks):
 * its Bank connection card while no Plaid keys are set (so the next step is right there),
 * otherwise its Linked institutions card.
 */
export function useLinkBankTo(): string {
  const { plaid } = useApp();
  return plaid && !plaid.configured ? '/settings/banks?focus=bank-connection' : '/settings/banks?focus=connections';
}

const TWELVE_HOURS = 12 * 60 * 60 * 1000;

export function AppProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [phase, setPhase] = useState<Phase>('loading');
  const [autoLockMinutes, setAutoLockMinutes] = useState(15);
  const [lockReason, setLockReason] = useState<LockReason>('start');
  const [elsewhere, setElsewhere] = useState<ElsewhereKind>('second');
  const [unreachable, setUnreachable] = useState<Unreachable | null>(null);
  const [plaid, setPlaid] = useState<PlaidStatus | null>(null);
  const [dataVersion, setDataVersion] = useState(0);
  const [syncing, setSyncing] = useState(false);
  const [attention, setAttention] = useState(0);
  const [updatedAt, setUpdatedAtState] = useState<string | null>(null);
  const setUpdatedAt = useCallback((iso: string | null) => setUpdatedAtState(iso), []);
  const [clock, setClock] = useState(() => new Date());
  const [authInfo, setAuthInfo] = useState<AuthInfo>(() => ({ supportContact: cachedSupportContact(), recovery: null, retryAfter: null, triesLeft: null }));
  const authInfoRef = useRef(authInfo);
  authInfoRef.current = authInfo;
  /** Increments per unlocked session so the auto-sync check runs once each. */
  const [sessionNo, setSessionNo] = useState(0);
  const [idleHeld, setIdleHeld] = useState(false);
  const holdIdle = useCallback((held: boolean) => setIdleHeld(held), []);
  const phaseRef = useRef<Phase>(phase);
  phaseRef.current = phase;
  const lockReasonRef = useRef<LockReason>(lockReason);
  lockReasonRef.current = lockReason;
  const syncingRef = useRef(false);
  const dupHandledRef = useRef(false);

  const invalidate = useCallback(() => setDataVersion((v) => v + 1), []);

  const refreshPlaid = useCallback(async () => {
    try {
      const ps = await api.plaid.status();
      if (phaseRef.current === 'unlocked') setPlaid(ps);
    } catch {
      /* 401 is handled globally; otherwise keep the last known status */
    }
  }, []);

  /** Go to the password screen. */
  const toLocked = useCallback((reason: LockReason) => {
    setPlaid(null);
    setAttention(0);
    setUpdatedAtState(null);
    setLockReason(reason);
    setPhase('locked');
  }, []);

  /** Go to "already open in another window". */
  const toElsewhere = useCallback((kind: ElsewhereKind) => {
    setSessionToken(null);
    setPlaid(null);
    setAttention(0);
    setElsewhere(kind);
    setPhase('elsewhere');
  }, []);

  const toUnreachable = useCallback((code: UnreachableCode) => {
    setUnreachable({ code, at: new Date() });
    setPhase('unreachable');
  }, []);

  /** The server's answer → phase (status at start and after a lost session). */
  const applyStatus = useCallback(
    (s: AuthStatus, fallback: LockReason) => {
      setAutoLockMinutes(s.auto_lock_minutes);
      setAuthInfo(authInfoFrom(s));
      if (!s.initialized) setPhase('setup');
      else if (s.unlocked) {
        setPhase('unlocked');
        setSessionNo((n) => n + 1);
      } else if (s.open_elsewhere) toElsewhere('second');
      else toLocked(reasonFromServer(s.lock_reason, fallback));
    },
    [toElsewhere, toLocked],
  );

  const loadStatus = useCallback(async () => {
    // A duplicated tab starts with a copy of another window's id and session token: give it its
    // own id and drop the token, so it opens at "already open" / the password, not in that session.
    if ((await ensureOwnTabId()) && !dupHandledRef.current) {
      dupHandledRef.current = true;
      setSessionToken(null);
    }
    try {
      applyStatus(await api.auth.status(FIRST_STATUS_TIMEOUT_MS), 'start');
    } catch {
      // No answer in 20 s, or no answer at all: the page loaded but FinTrack doesn't answer.
      toUnreachable('FT-START-02');
    }
  }, [applyStatus, toUnreachable]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  // A 401 from anywhere: the vault locked (→ password), or another window took over (→ "moved").
  useEffect(
    () =>
      onLocked((why: SessionEnd) => {
        if (phaseRef.current !== 'unlocked') return;
        if (why === 'moved') {
          toElsewhere('moved');
          return;
        }
        toLocked('ended');
        // Say why if the server knows (away too long, window closed, locked from elsewhere).
        api.auth
          .status()
          .then((s) => {
            setAuthInfo(authInfoFrom(s));
            if (phaseRef.current !== 'locked' || lockReasonRef.current !== 'ended') return;
            if (s.open_elsewhere) toElsewhere('moved');
            else setLockReason(reasonFromServer(s.lock_reason, 'ended'));
          })
          .catch(() => {});
      }),
    [toElsewhere, toLocked],
  );

  // Two requests in a row without an answer: is FinTrack still running? (FT-RUN-01)
  const probingRef = useRef(false);
  useEffect(
    () =>
      onConnectionLost(() => {
        const ph = phaseRef.current;
        if (ph === 'loading' || ph === 'unreachable' || probingRef.current) return;
        probingRef.current = true;
        api.app
          .health()
          .catch(() => {
            if (phaseRef.current !== 'unreachable' && phaseRef.current !== 'loading') toUnreachable('FT-RUN-01');
          })
          .finally(() => {
            probingRef.current = false;
          });
      }),
    [toUnreachable],
  );

  // Only ever from the open app (Lock button, idle timer): never from the password or "already
  // open" screens, where a lock without a session would lock FinTrack in the other window too.
  const lockNow = useCallback(
    async (reason: 'manual' | 'idle' = 'manual') => {
      if (phaseRef.current !== 'unlocked') return;
      try {
        await api.auth.lock(reason);
      } catch {
        /* already locked server-side, or offline: lock the UI regardless */
      }
      toLocked(reason);
      // The server ignores a lock from a window whose session already moved to another window
      // (that one stays open): say so instead of "You locked FinTrack".
      api.auth
        .status()
        .then((s) => {
          if (phaseRef.current === 'locked' && lockReasonRef.current === reason && s.open_elsewhere) toElsewhere('moved');
        })
        .catch(() => {});
    },
    [toElsewhere, toLocked],
  );

  /** Open FinTrack in this window. `reload`: already open, but the session changed (a restore). */
  const openUnlocked = useCallback((reload: boolean) => {
    // Once per unlock: a handoff answer and the late-answer listener can both arrive, and a
    // second call would bump the session and tell the other windows again. Set synchronously
    // so a second call in the same tick sees it before React re-renders.
    if (phaseRef.current === 'unlocked' && !reload) return;
    phaseRef.current = 'unlocked';
    clearHandoffNonce();
    setLockReason('start');
    setPhase('unlocked');
    setSessionNo((n) => n + 1);
    invalidate();
    postTabs({ type: 'unlocked', tab: getTabId() });
    // Refresh auto_lock_minutes in case the server config changed.
    api.auth
      .status()
      .then((s) => {
        setAutoLockMinutes(s.auto_lock_minutes);
        setAuthInfo(authInfoFrom(s));
      })
      .catch(() => {});
  }, [invalidate]);
  const enterUnlocked = useCallback(() => openUnlocked(false), [openUnlocked]);
  const reloadUnlocked = useCallback(() => openUnlocked(true), [openUnlocked]);

  const adoptSession = useCallback(
    (token: string) => {
      if (phaseRef.current === 'unlocked') return; // already taken (see openUnlocked)
      setSessionToken(token);
      enterUnlocked();
    },
    [enterUnlocked],
  );

  const showPasswordHere = useCallback(() => toLocked('here'), [toLocked]);

  // The heartbeat's answer: notice when the session ended, moved, or the other window went away.
  const onPresence = useCallback(
    (p: Presence) => {
      const ph = phaseRef.current;
      if (ph === 'unlocked' && !p.unlocked) {
        if (p.open_elsewhere) toElsewhere('moved');
        else {
          setSessionToken(null);
          toLocked(reasonFromServer(p.lock_reason, 'ended'));
        }
      } else if (ph === 'elsewhere' && !p.open_elsewhere && !p.unlocked) {
        toLocked(reasonFromServer(p.lock_reason, 'start'));
      } else if (ph === 'locked' && p.open_elsewhere && lockReasonRef.current !== 'here') {
        toElsewhere('second');
      }
    },
    [toElsewhere, toLocked],
  );

  usePresence({ enabled: phase !== 'loading' && phase !== 'unreachable', onPresence });

  // Other FinTrack windows: hand the session over (Open here), and notice their unlocks.
  useEffect(() => {
    const me = getTabId();
    return listenTabs((msg) => {
      const ph = phaseRef.current;
      switch (msg.type) {
        case 'handoff-request': {
          if (msg.from === me || ph !== 'unlocked') return;
          api.app
            .handoff(msg.from)
            .then(({ session_token }) => {
              postTabs({ type: 'handoff', to: msg.from, nonce: msg.nonce, token: session_token });
              toElsewhere('moved');
            })
            .catch(() => postTabs({ type: 'handoff-refused', to: msg.from, nonce: msg.nonce }));
          return;
        }
        case 'handoff': {
          // A late answer (after the 1.5 s limit) to this window's own request is still taken.
          if (
            msg.to === me &&
            msg.nonce === pendingHandoffNonce() &&
            isHandoffToken(msg.token) &&
            (ph === 'locked' || ph === 'elsewhere')
          )
            adoptSession(msg.token);
          return;
        }
        case 'unlocked': {
          if (msg.tab === me) return;
          if (ph === 'unlocked') toElsewhere('moved');
          else if (ph === 'locked' && lockReasonRef.current !== 'here') toElsewhere('second');
          return;
        }
        case 'focus-request': {
          if (ph === 'unlocked') window.focus();
          return;
        }
        default:
          return;
      }
    });
  }, [adoptSession, toElsewhere]);

  const refreshAuthInfo = useCallback(async (): Promise<AuthInfo> => {
    try {
      const info = authInfoFrom(await api.auth.status());
      setAuthInfo(info);
      return info;
    } catch {
      return authInfoRef.current;
    }
  }, []);

  const sync = useCallback(
    async (opts: SyncOptions = {}): Promise<SyncItemResult[] | null> => {
      if (syncingRef.current) return null;
      syncingRef.current = true;
      setSyncing(true);
      try {
        const { results } = await api.plaid.sync(opts.itemId);
        invalidate();
        const allOk = results.every((r) => r.ok);
        if (opts.quiet === 'all' || (opts.quiet === 'success' && allOk && results.length > 0)) {
          /* the caller says what happened */
        } else if (results.length === 0) {
          if (!opts.auto) {
            toast.push({ tone: 'info', title: 'Nothing to sync', body: 'Link a bank in Settings to pull balances and transactions automatically.' });
          }
        } else {
          toast.push(syncToast(results, !!opts.auto));
        }
        return results;
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 401)) {
          toast.push({ tone: 'error', title: 'Sync failed', body: errorMessage(e) });
        }
        return null;
      } finally {
        syncingRef.current = false;
        setSyncing(false);
      }
    },
    [invalidate, toast],
  );

  // After each unlock: learn Plaid status; auto-sync if stale (>12h or never).
  useEffect(() => {
    if (phase !== 'unlocked' || sessionNo === 0) return;
    let cancelled = false;
    (async () => {
      try {
        const [ps, summary, items] = await Promise.all([api.plaid.status(), api.summary(), api.plaid.items()]);
        if (cancelled) return;
        setPlaid(ps);
        setAttention(summary.items_needing_attention);
        setUpdatedAtState(summary.last_synced_at);
        if (!ps.configured || items.length === 0) return;
        const last = summary.last_synced_at ? Date.parse(summary.last_synced_at) : NaN;
        const stale = Number.isNaN(last) || Date.now() - last > TWELVE_HOURS;
        if (stale) void sync({ auto: true });
      } catch {
        /* 401 is handled globally; other failures just skip auto-sync */
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionNo]);

  // The "Updated …" clock: once a minute while FinTrack is open.
  useEffect(() => {
    if (phase !== 'unlocked') return;
    setClock(new Date());
    const h = window.setInterval(() => setClock(new Date()), 60_000);
    return () => window.clearInterval(h);
  }, [phase]);
  const updated = useMemo<Updated>(() => ({ updatedAt, text: formatUpdated(updatedAt, clock), now: clock }), [updatedAt, clock]);

  // Client-side idle timer mirroring the server's auto-lock.
  useIdleTimer({
    enabled: phase === 'unlocked' && autoLockMinutes > 0 && !idleHeld,
    timeoutMs: autoLockMinutes * 60_000,
    onIdle: () => void lockNow('idle'),
  });

  const value = useMemo<AppState>(
    () => ({
      phase,
      autoLockMinutes,
      setAutoLockMinutes,
      lockReason,
      elsewhere,
      unreachable,
      showPasswordHere,
      adoptSession,
      plaid,
      refreshPlaid,
      dataVersion,
      invalidate,
      syncing,
      sync,
      lockNow,
      enterUnlocked,
      reloadUnlocked,
      retryStatus: () => {
        setPhase('loading');
        void loadStatus();
      },
      attention,
      setAttention,
      setUpdatedAt,
      authInfo,
      refreshAuthInfo,
      sessionNo,
      holdIdle,
      showUnreachable: toUnreachable,
    }),
    [
      phase,
      autoLockMinutes,
      lockReason,
      elsewhere,
      unreachable,
      showPasswordHere,
      adoptSession,
      plaid,
      refreshPlaid,
      dataVersion,
      invalidate,
      syncing,
      sync,
      lockNow,
      enterUnlocked,
      reloadUnlocked,
      loadStatus,
      attention,
      setUpdatedAt,
      authInfo,
      refreshAuthInfo,
      sessionNo,
      holdIdle,
      toUnreachable,
    ],
  );

  return (
    <Ctx.Provider value={value}>
      <UpdatedCtx.Provider value={updated}>{children}</UpdatedCtx.Provider>
    </Ctx.Provider>
  );
}

function syncToast(results: SyncItemResult[], auto: boolean) {
  const failed = results.filter((r) => !r.ok);
  if (failed.length) {
    // Plain words, no counts: Home's "Things that need you" says which bank and what to do.
    return {
      tone: 'warn' as const,
      title: failed.length === 1 ? 'One bank needs attention' : `${failed.length} banks need attention`,
      timeout: 0,
      body: 'Home shows what to do next.',
    };
  }
  const added = results.reduce((s, r) => s + r.transactions_added, 0);
  const accounts = results.reduce((s, r) => s + r.accounts, 0);
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const lead = [
    accounts > 0 ? `${plural(accounts, 'account')} updated` : null,
    added > 0 ? `${plural(added, 'new transaction')}` : 'No new transactions',
  ]
    .filter(Boolean)
    .join(' · ');
  return {
    tone: 'success' as const,
    title: auto ? 'Accounts refreshed' : 'Sync complete',
    timeout: 6000,
    body: (
      <>
        <div>{lead}</div>
        {results.length > 1 ? (
          <ul className="sync-lines">
            {results.map((r) => (
              <li key={r.item_id}>
                <span className="name truncate">{r.institution_name ?? 'Connection'}</span>
                <span className="num">
                  {r.transactions_added > 0 ? `${r.transactions_added} new` : 'Up to date'}
                  {r.holdings ? ` · ${r.holdings} holdings` : ''}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
      </>
    ),
  };
}
