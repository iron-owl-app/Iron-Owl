import { useEffect, useRef } from 'react';
import { api, ApiError, sessionGeneration, type Presence } from '../api';

const BEAT_MS = 30_000;
/** Coming back to the front sooner than this after the last beat doesn't send another. */
const MIN_GAP_MS = 5_000;
/** The first beat, soon after the window opens (after a reload it confirms the window is back). */
const FIRST_BEAT_MS = 1_000;

/**
 * The window's heartbeat (MAPPING "Already open" model): POST /api/app/alive every 30 s and
 * whenever the window comes back to the front, in every phase that talks to FinTrack (open,
 * locked, sign in, "already open"). The server uses it to know which windows are still there;
 * it never counts as activity, so it can't stop FinTrack locking itself when the user is away.
 *
 * On pagehide (not kept in the back/forward cache) it tells the server the window is going away;
 * the server locks FinTrack after a short grace unless the same window comes back (a reload).
 */
export function usePresence({ enabled, onPresence }: { enabled: boolean; onPresence: (p: Presence) => void }) {
  const onPresenceRef = useRef(onPresence);
  onPresenceRef.current = onPresence;

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let inFlight = false;
    let last = 0;

    const beat = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      last = Date.now();
      const gen = sessionGeneration();
      try {
        const p = await api.app.alive();
        // Sent before this window unlocked or handed its session over: the answer is stale.
        if (!stopped && p && typeof p === 'object' && gen === sessionGeneration()) onPresenceRef.current(p);
      } catch (e) {
        // An older FinTrack without heartbeats: stop asking. Network errors are counted in api.ts.
        if (e instanceof ApiError && (e.status === 404 || e.status === 405)) stopped = true;
      } finally {
        inFlight = false;
      }
    };
    const soon = () => {
      if (Date.now() - last >= MIN_GAP_MS) void beat();
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') soon();
    };
    const onPageHide = (e: PageTransitionEvent) => {
      if (!e.persisted) void api.app.closing().catch(() => {});
    };

    const first = window.setTimeout(() => void beat(), FIRST_BEAT_MS);
    const interval = window.setInterval(() => void beat(), BEAT_MS);
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('focus', soon);
    window.addEventListener('pagehide', onPageHide);
    return () => {
      stopped = true;
      window.clearTimeout(first);
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('focus', soon);
      window.removeEventListener('pagehide', onPageHide);
    };
  }, [enabled]);
}
