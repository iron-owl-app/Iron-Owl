import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  api,
  ApiError,
  apiErrorCode,
  updateHelp,
  type UpdateCheck,
  type UpdateHelpReason,
  type UpdateOffer,
  type UpdateProgress,
  type UpdateStatus,
  type UpdateStep,
} from '../api';
import { useApp } from '../state';
import { useToast } from '../components/Toast';
import { BANNER, MESSAGES } from './copy';
import { afterPollError, HEALTH_POLL_AFTER_LIMIT_MS, HEALTH_POLL_MS, restartedBack, restartOverdue } from './watch';
import './updates.css';

/**
 * Updates (design D5): what FinTrack knows about updates, and where the install flow is.
 *
 * Lives inside AppProvider and above Gate (App.tsx), so the install screens can stay on top
 * while FinTrack restarts and the window has no session. Flows:
 *   idle · news (What's new pop-up) · check (a picked file that won't be installed)
 *   progress (backup → install → restart, then waiting for FinTrack to answer again)
 *   failed (state 5) · help (state 6) · rollingBack (data update failed after sign-in)
 */
export type UpdateFlow =
  | { kind: 'idle' }
  | { kind: 'news'; offer: UpdateOffer }
  | { kind: 'check'; check: Exclude<UpdateCheck, { result: 'ready' }> }
  | { kind: 'progress'; toVersion: string; minutes: number; step: UpdateStep; restarting: boolean; finished: boolean }
  | { kind: 'failed'; code: string; at: string; backupKept: boolean }
  /** `ackAt`: the install stopped here (progress `failed` with an FT-UPD-HELP-* code), acknowledged on Back. */
  | { kind: 'help'; code: string; reason: UpdateHelpReason | null; version: string; ackAt?: string }
  | { kind: 'rollingBack'; toVersion: string };

/** Flows shown as a full window (instead of the app or the password screen). */
export const FULL_WINDOW_FLOWS: UpdateFlow['kind'][] = ['progress', 'failed', 'help', 'rollingBack'];

interface UpdateApi {
  status: UpdateStatus | null;
  /** The last status read failed (and there's none to show). */
  statusError: boolean;
  /** Support contact for the words ("Sam"); null when none is set. */
  contact: string | null;
  flow: UpdateFlow;
  /** Show the banner (decision 1). */
  bannerVisible: boolean;
  /** The file being checked (Settings: "Checking {file}…"). */
  checkingFile: string | null;
  /** An install request is on its way (What's new: "Starting…"). */
  starting: boolean;
  refresh: () => Promise<void>;
  /** GitHub source: Settings "Check now". False when the request itself failed. */
  checkNow: () => Promise<boolean>;
  /** GitHub source: Settings "Check for updates once a day". False when it didn't save. */
  setAutoCheck: (on: boolean) => Promise<boolean>;
  /** Open What's new for the current offer (banner, Settings). */
  openNews: () => void;
  /** Later / Esc: only closes the pop-up (the banner stays). */
  closeNews: () => void;
  /** Banner › Not now: hide it until tomorrow. */
  notNow: () => void;
  install: () => void;
  checkFile: (file: File) => void;
  closeCheck: () => void;
  /** "Back to FinTrack" on the didn't-install and needs-help screens. */
  backToApp: () => void;
  /** Unlock said 409 update_rolled_back: FinTrack is going back to `toVersion`. */
  enterRollingBack: (toVersion: string) => void;
}

const Ctx = createContext<UpdateApi | null>(null);

export function useUpdates(): UpdateApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useUpdates outside UpdateProvider');
  return ctx;
}

const SCAN_THROTTLE_MS = 30_000;
const STATUS_POLL_MS = 5 * 60_000;
const PROGRESS_POLL_MS = 1_000;
/** A result already shown before sign-in (didn't start, rolled back): acknowledged quietly after unlock. */
const SHOWN_KEY = 'ft.update.shownResult';

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

function sameVersion(a: string | null | undefined, b: string | null | undefined): boolean {
  const norm = (v: string | null | undefined) => (v ?? '').trim().replace(/^v/i, '');
  return !!a && !!b && norm(a) === norm(b);
}

/** Remember a result shown before sign-in (single use, read at the next unlock). */
function remember(code: string) {
  try {
    sessionStorage.setItem(SHOWN_KEY, JSON.stringify({ code, at: Date.now() }));
  } catch {
    /* storage blocked: the result may show once more after sign-in */
  }
}
/** Was this result already shown here? Only when the codes match and the times are close. */
function alreadyShown(code: string | null, at: string): boolean {
  let raw: string | null = null;
  try {
    raw = sessionStorage.getItem(SHOWN_KEY);
    sessionStorage.removeItem(SHOWN_KEY);
  } catch {
    return false;
  }
  if (!raw || !code) return false;
  try {
    const m = JSON.parse(raw) as { code?: unknown; at?: unknown };
    const when = Date.parse(at);
    return m.code === code && typeof m.at === 'number' && Number.isFinite(when) && Math.abs(when - m.at) < 10 * 60_000;
  } catch {
    return false;
  }
}

/** Dev mock only (`&update=news|help`): open What's new once the offer is known. */
const DEV_OPEN_NEWS = import.meta.env.DEV && ['news', 'help'].includes(new URLSearchParams(window.location.search).get('update') ?? '');

export function UpdateProvider({ children }: { children: ReactNode }) {
  const { phase, sessionNo, authInfo, holdIdle, retryStatus, showUnreachable } = useApp();
  const toast = useToast();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [statusError, setStatusError] = useState(false);
  const [flow, setFlow] = useState<UpdateFlow>({ kind: 'idle' });
  const [checkingFile, setCheckingFile] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [hidden, setHidden] = useState<string | null>(null);

  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const statusRef = useRef(status);
  statusRef.current = status;
  const flowRef = useRef(flow);
  flowRef.current = flow;
  /** Bumped to stop a running watch loop (a newer one started, or unmounted). */
  const watchGen = useRef(0);
  const lastScan = useRef(0);
  const devOpened = useRef(false);

  const contact = status?.support_contact?.trim() || authInfo.supportContact;

  useEffect(
    () => () => {
      watchGen.current += 1;
    },
    [],
  );

  const refresh = useCallback(async () => {
    if (phaseRef.current !== 'unlocked') return;
    try {
      const st = await api.update.status();
      setStatus(st);
      setStatusError(false);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) setStatusError(true);
    }
  }, []);

  const scan = useCallback(async (force = false) => {
    if (phaseRef.current !== 'unlocked' || statusRef.current?.mode !== 'enabled') return;
    if (!force && Date.now() - lastScan.current < SCAN_THROTTLE_MS) return;
    lastScan.current = Date.now();
    try {
      setStatus(await api.update.scan());
      setStatusError(false);
    } catch {
      /* 401 is handled globally; 403 or offline: keep what we have */
    }
  }, []);

  const checkNow = useCallback(async () => {
    if (phaseRef.current !== 'unlocked') return false;
    try {
      setStatus(await api.update.checkNow());
      setStatusError(false);
      return true;
    } catch {
      return false; // 401 is handled globally
    }
  }, []);

  const setAutoCheck = useCallback(async (on: boolean) => {
    if (phaseRef.current !== 'unlocked') return false;
    try {
      setStatus(await api.update.setAutoCheck(on));
      return true;
    } catch {
      return false;
    }
  }, []);

  const ack = useCallback((at: string | undefined) => {
    if (!at) return;
    api.update.ackResult(at).catch(() => {});
  }, []);

  // ------------------------------------------------------------ watching an install / a rollback

  const toFailed = useCallback((code: string | null, at: string | null, backupKept: boolean) => {
    setFlow({ kind: 'failed', code: code || 'FT-UPD-02', at: at || new Date().toISOString(), backupKept });
  }, []);

  /**
   * The install stopped before the restart (FT-UPD-01 backup, -02 files). Progress doesn't say
   * whether a backup was made; the result does ("The backup was kept." only when it was).
   */
  const failedFromProgress = useCallback(
    async (p: UpdateProgress) => {
      if (p.code?.startsWith('FT-UPD-HELP')) {
        // Found while unpacking (e.g. the disk filled up): needs help, not a failed update.
        let reason: UpdateHelpReason | null = null;
        try {
          const st = await api.update.status();
          setStatus(st);
          if (st.offer?.help?.code === p.code) reason = st.offer.help.reason;
        } catch {
          reason = null;
        }
        setFlow({ kind: 'help', code: p.code, reason, version: p.to_version, ackAt: p.at });
        return;
      }
      let kept = false;
      try {
        const st = await api.update.status();
        setStatus(st);
        const r = st.last_result;
        if (r && r.outcome !== 'installed' && (!p.code || r.code === p.code)) kept = r.backup_kept;
      } catch {
        kept = false;
      }
      toFailed(p.code, p.at, kept);
    },
    [toFailed],
  );

  /**
   * Wait for FinTrack to answer again after it restarts: a new boot id means it's back. The
   * version then says whether the update is running (→ Sign in) or the launcher went back to
   * the previous one (`onPrevious`). Past the limit "FinTrack isn't running" (FT-UPD-04) is
   * shown, and the window keeps asking (slower): if FinTrack comes back it carries on by itself.
   */
  const waitForRestart = useCallback(
    async (gen: number, boot0: string | null, since: number, onBack: (version: string) => void) => {
      let sawDown = boot0 === null;
      let overdue = false;
      while (watchGen.current === gen) {
        await sleep(overdue ? HEALTH_POLL_AFTER_LIMIT_MS : HEALTH_POLL_MS);
        if (watchGen.current !== gen) return;
        let h: { boot_id: string; version: string } | null = null;
        try {
          h = await api.health();
        } catch {
          h = null;
        }
        if (watchGen.current !== gen) return;
        if (restartedBack(boot0, h, sawDown) && h) {
          onBack(h.version);
          return;
        }
        if (!h) sawDown = true;
        if (restartOverdue(since, Date.now(), overdue)) {
          overdue = true;
          setFlow({ kind: 'idle' });
          showUnreachable('FT-UPD-04');
        }
      }
    },
    [showUnreachable],
  );

  /** Follow an install from its first answer to Sign in (or state 5). */
  const watchInstall = useCallback(
    async (first: UpdateProgress, minutes: number, bootBefore: string | null) => {
      const gen = ++watchGen.current;
      const toVersion = first.to_version;
      const set = (step: UpdateStep, restarting: boolean, finished = false) =>
        setFlow({ kind: 'progress', toVersion, minutes, step, restarting, finished });

      if (first.state === 'failed') {
        await failedFromProgress(first);
        return;
      }
      let boot0 = bootBefore;
      if (boot0 === null) {
        try {
          boot0 = (await api.health()).boot_id;
        } catch {
          boot0 = null;
        }
      }
      let restarting = first.state === 'restarting' || first.step === 'restart';
      set(first.step, restarting);
      let empty = 0;
      while (!restarting && watchGen.current === gen) {
        await sleep(PROGRESS_POLL_MS);
        if (watchGen.current !== gen) return;
        let p: UpdateProgress | undefined;
        try {
          p = await api.update.progress(true);
        } catch (e) {
          // No answer, or the session is gone. Only a FinTrack that doesn't answer, or a new
          // one (another boot id), is restarting; the same one still answering is not.
          let h: { boot_id: string } | null = null;
          try {
            h = await api.health();
          } catch {
            h = null;
          }
          if (watchGen.current !== gen) return;
          const action = afterPollError(boot0, h, e instanceof ApiError && e.status === 401);
          if (action === 'retry') continue;
          if (action === 'signIn') {
            // This window's session ended (locked, or another window): sign in again; the
            // status read after sign-in picks the install up (running, or its result).
            watchGen.current += 1;
            setFlow({ kind: 'idle' });
            retryStatus();
            return;
          }
          restarting = true;
          break;
        }
        if (watchGen.current !== gen) return;
        if (!p) {
          // Nothing running any more. Three in a row: ask what happened.
          empty += 1;
          if (empty < 3) continue;
          let st: UpdateStatus | null = null;
          try {
            st = await api.update.status();
          } catch (e) {
            let h: { boot_id: string } | null = null;
            try {
              h = await api.health();
            } catch {
              h = null;
            }
            if (watchGen.current !== gen) return;
            const action = afterPollError(boot0, h, e instanceof ApiError && e.status === 401);
            if (action === 'retry') continue;
            if (action === 'signIn') {
              watchGen.current += 1;
              setFlow({ kind: 'idle' });
              retryStatus();
              return;
            }
            restarting = true;
            break;
          }
          if (watchGen.current !== gen) return;
          setStatus(st);
          if (st.install) {
            empty = 0;
            p = st.install;
          } else {
            const r = st.last_result;
            if (r && r.outcome !== 'installed' && sameVersion(r.to_version, toVersion)) toFailed(r.code, r.at, r.backup_kept);
            else setFlow({ kind: 'idle' });
            return;
          }
        }
        empty = 0;
        if (p.state === 'failed') {
          if (watchGen.current === gen) await failedFromProgress(p);
          return;
        }
        restarting = p.state === 'restarting' || p.step === 'restart';
        set(p.step, restarting);
      }
      if (watchGen.current !== gen) return;
      set('restart', true);
      await waitForRestart(gen, boot0, Date.now(), (version) => {
        if (sameVersion(version, toVersion)) {
          // The new version is up: tick the last step, then Sign in.
          set('restart', true, true);
          window.setTimeout(() => {
            if (watchGen.current !== gen) return;
            setFlow({ kind: 'idle' });
            retryStatus();
          }, 900);
        } else {
          // The launcher went back to the previous version (FT-UPD-03).
          remember('FT-UPD-03');
          toFailed('FT-UPD-03', new Date().toISOString(), true);
          retryStatus();
        }
      });
    },
    [failedFromProgress, retryStatus, toFailed, waitForRestart],
  );

  const enterRollingBack = useCallback(
    (toVersion: string) => {
      const gen = ++watchGen.current;
      setFlow({ kind: 'rollingBack', toVersion });
      void (async () => {
        let boot0: string | null = null;
        try {
          boot0 = (await api.health()).boot_id;
        } catch {
          boot0 = null;
        }
        if (watchGen.current !== gen) return;
        await waitForRestart(gen, boot0, Date.now(), () => {
          remember('FT-UPD-05');
          toFailed('FT-UPD-05', new Date().toISOString(), true);
          retryStatus();
        });
      })();
    },
    [retryStatus, toFailed, waitForRestart],
  );

  // ------------------------------------------------------------ each unlock

  /** What the last update did, once per sign-in: the success message, or state 5. */
  const handleResult = useCallback(
    (st: UpdateStatus) => {
      const r = st.last_result;
      if (!r) return;
      if (r.outcome === 'installed') {
        toast.push({ tone: 'success', title: MESSAGES.upToDate(r.to_version), timeout: 6000 });
        ack(r.at);
        return;
      }
      if (alreadyShown(r.code, r.at)) {
        // Already shown before sign-in (FinTrack went back to the previous version).
        ack(r.at);
        return;
      }
      toFailed(r.code, r.at, r.backup_kept);
    },
    [ack, toFailed, toast],
  );

  useEffect(() => {
    if (phase !== 'unlocked') return;
    let cancelled = false;
    void (async () => {
      let st: UpdateStatus;
      try {
        st = await api.update.status();
      } catch (e) {
        if (!cancelled && !(e instanceof ApiError && e.status === 401)) setStatusError(true);
        return;
      }
      if (cancelled) return;
      setStatus(st);
      setStatusError(false);
      if (st.install && st.install.state !== 'failed') {
        // An install is running (the window was reloaded): follow it.
        void watchInstall(st.install, st.offer?.minutes ?? 1, null);
        return;
      }
      if (FULL_WINDOW_FLOWS.includes(flowRef.current.kind)) return;
      handleResult(st);
      if (st.mode === 'enabled') {
        lastScan.current = 0;
        void scan(true);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Once per unlocked session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionNo, phase === 'unlocked']);

  // Look in Downloads again when the user comes back to the window (at most every 30 s), and re-read
  // the status every 5 minutes while unlocked (the server looks by itself too).
  useEffect(() => {
    if (phase !== 'unlocked') return;
    const again = () => {
      if (document.visibilityState === 'visible') void scan();
    };
    window.addEventListener('focus', again);
    document.addEventListener('visibilitychange', again);
    const t = window.setInterval(() => void refresh(), STATUS_POLL_MS);
    return () => {
      window.removeEventListener('focus', again);
      document.removeEventListener('visibilitychange', again);
      window.clearInterval(t);
    };
  }, [phase, refresh, scan]);

  // Locked or gone: pop-ups close (full-window flows stay; they don't need a session).
  useEffect(() => {
    if (phase === 'unlocked') return;
    setFlow((f) => (f.kind === 'news' || f.kind === 'check' ? { kind: 'idle' } : f));
    setCheckingFile(null);
  }, [phase]);

  // The idle timer waits while an install screen is up.
  const fullWindow = FULL_WINDOW_FLOWS.includes(flow.kind);
  useEffect(() => {
    holdIdle(fullWindow);
  }, [fullWindow, holdIdle]);
  useEffect(() => () => holdIdle(false), [holdIdle]);

  // ------------------------------------------------------------ actions

  const openNews = useCallback(() => {
    const offer = statusRef.current?.offer;
    if (!offer) return;
    if (offer.can_install) setFlow({ kind: 'news', offer });
    else setFlow({ kind: 'help', code: offer.help?.code ?? 'FT-UPD-HELP', reason: offer.help?.reason ?? null, version: offer.version });
  }, []);

  useEffect(() => {
    if (!DEV_OPEN_NEWS || devOpened.current || phase !== 'unlocked' || !status?.offer?.can_install) return;
    devOpened.current = true;
    openNews();
  }, [phase, status, openNews]);

  const closeNews = useCallback(() => {
    setFlow((f) => (f.kind === 'news' ? { kind: 'idle' } : f));
  }, []);

  const notNow = useCallback(() => {
    const offer = statusRef.current?.offer;
    if (!offer) return;
    setHidden(offer.id);
    toast.push({ tone: 'info', title: BANNER.remindTitle, body: BANNER.remindBody, timeout: 6000 });
    document.getElementById('main')?.focus({ preventScroll: true });
    api.update
      .dismiss(offer.id)
      .then((st) => setStatus(st))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 404) void refresh();
      });
  }, [refresh, toast]);

  const startingRef = useRef(false);
  /** A picked file is being checked (the ref: read inside callbacks). */
  const checkingRef = useRef(false);
  const install = useCallback(() => {
    const f = flowRef.current;
    if (f.kind !== 'news' || startingRef.current || checkingRef.current) return;
    const offer = f.offer;
    startingRef.current = true;
    setStarting(true);
    void (async () => {
      let boot0: string | null = null;
      try {
        boot0 = (await api.health()).boot_id;
      } catch {
        boot0 = null;
      }
      try {
        const p = await api.update.install(offer.id);
        void watchInstall(p, offer.minutes, boot0);
      } catch (e) {
        const code = apiErrorCode(e);
        const detail = e instanceof ApiError ? e.detail : '';
        if (e instanceof ApiError && e.status === 409 && (code === 'needs_help' || detail === 'needs_help' || code?.startsWith('FT-UPD-HELP'))) {
          const h = updateHelp(e) ?? offer.help;
          setFlow({ kind: 'help', code: h?.code ?? code ?? 'FT-UPD-HELP', reason: h?.reason ?? null, version: offer.version });
        } else if (e instanceof ApiError && e.status === 409 && (detail === 'busy' || code === 'busy')) {
          // One is already running (another window started it): follow that one.
          try {
            const p = await api.update.progress();
            if (p) void watchInstall(p, offer.minutes, boot0);
            else {
              setFlow({ kind: 'idle' });
              toast.push({ tone: 'info', title: MESSAGES.alreadyRunning });
            }
          } catch {
            setFlow({ kind: 'idle' });
          }
        } else if (e instanceof ApiError && e.status === 409) {
          setFlow({ kind: 'idle' });
          toast.push({ tone: 'info', title: MESSAGES.disabled });
          void refresh();
        } else if (e instanceof ApiError && e.status === 404) {
          setFlow({ kind: 'idle' });
          toast.push({ tone: 'info', title: MESSAGES.stale });
          void refresh();
        } else if (!(e instanceof ApiError && e.status === 401)) {
          toast.push({ tone: 'error', title: MESSAGES.installFailed });
        }
      } finally {
        startingRef.current = false;
        setStarting(false);
      }
    })();
  }, [refresh, toast, watchInstall]);

  const checkFile = useCallback(
    (file: File) => {
      checkingRef.current = true;
      setCheckingFile(file.name);
      void (async () => {
        try {
          const r = await api.update.checkFile(file);
          if (r.result === 'ready') {
            void refresh();
            if (r.offer.can_install) setFlow({ kind: 'news', offer: r.offer });
            else
              setFlow({
                kind: 'help',
                code: r.offer.help?.code ?? 'FT-UPD-HELP',
                reason: r.offer.help?.reason ?? null,
                version: r.offer.version,
              });
          } else {
            setFlow({ kind: 'check', check: r });
          }
        } catch (e) {
          if (e instanceof ApiError && e.status === 413) {
            setFlow({ kind: 'check', check: { result: 'rejected', reason: 'too_large', code: 'FT-UPD-BIG', file_name: file.name } });
          } else if (e instanceof ApiError && e.status === 429) {
            toast.push({ tone: 'info', title: MESSAGES.busy });
          } else if (e instanceof ApiError && e.status === 409 && apiErrorCode(e) === 'busy') {
            toast.push({ tone: 'info', title: MESSAGES.alreadyRunning });
          } else if (e instanceof ApiError && e.status === 409) {
            toast.push({ tone: 'info', title: MESSAGES.disabled });
          } else if (!(e instanceof ApiError && e.status === 401)) {
            toast.push({ tone: 'error', title: MESSAGES.checkFailed });
          }
        } finally {
          checkingRef.current = false;
          setCheckingFile(null);
        }
      })();
    },
    [refresh, toast],
  );

  const closeCheck = useCallback(() => {
    setFlow((f) => (f.kind === 'check' ? { kind: 'idle' } : f));
  }, []);

  const backToApp = useCallback(() => {
    const f = flowRef.current;
    watchGen.current += 1;
    setFlow({ kind: 'idle' });
    if (phaseRef.current !== 'unlocked') return;
    if (f.kind === 'help' && f.ackAt) {
      // The stopped install was seen: the server stops reporting it.
      api.update
        .ackResult(f.ackAt)
        .then(() => refresh())
        .catch(() => {});
      return;
    }
    if (f.kind !== 'failed') return;
    // The result was read: don't show it again after the next sign-in.
    void api.update
      .status()
      .then((st) => {
        setStatus(st);
        const r = st.last_result;
        if (r && r.outcome !== 'installed') {
          api.update
            .ackResult(r.at)
            .then(() => refresh())
            .catch(() => {});
        }
      })
      .catch(() => {});
  }, [refresh]);

  const offer = status?.offer ?? null;
  const bannerVisible =
    phase === 'unlocked' &&
    status?.mode === 'enabled' &&
    !!offer &&
    offer.can_install &&
    status.banner.show &&
    hidden !== offer.id &&
    // A picked file is being checked: it may become the offer in a moment (the server also
    // refuses to start an install meanwhile), so no Install button until it's done.
    checkingFile === null &&
    !(status.install && status.install.state !== 'failed') &&
    !FULL_WINDOW_FLOWS.includes(flow.kind);

  const value = useMemo<UpdateApi>(
    () => ({
      status,
      statusError,
      contact,
      flow,
      bannerVisible,
      checkingFile,
      starting,
      refresh,
      checkNow,
      setAutoCheck,
      openNews,
      closeNews,
      notNow,
      install,
      checkFile,
      closeCheck,
      backToApp,
      enterRollingBack,
    }),
    [status, statusError, contact, flow, bannerVisible, checkingFile, starting, refresh, checkNow, setAutoCheck, openNews, closeNews, notNow, install, checkFile, closeCheck, backToApp, enterRollingBack],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
