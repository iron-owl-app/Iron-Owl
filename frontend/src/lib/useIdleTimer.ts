import { useEffect, useRef } from 'react';

const ACTIVITY_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart', 'scroll', 'focus'] as const;

/**
 * Calls `onIdle` once after `timeoutMs` without user activity. Uses wall-clock
 * comparison on a coarse interval (and on tab re-focus), so it still fires
 * correctly after the laptop sleeps or the tab is throttled in the background.
 */
export function useIdleTimer({ enabled, timeoutMs, onIdle }: { enabled: boolean; timeoutMs: number; onIdle: () => void }) {
  const onIdleRef = useRef(onIdle);
  onIdleRef.current = onIdle;

  useEffect(() => {
    if (!enabled || timeoutMs <= 0) return;
    let last = Date.now();
    let fired = false;

    const check = () => {
      if (!fired && Date.now() - last >= timeoutMs) {
        fired = true;
        onIdleRef.current();
      }
    };
    // Check before bumping: activity after a long sleep must not "rescue" an
    // already-expired session.
    const bump = () => {
      check();
      if (!fired) last = Date.now();
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') check();
    };

    ACTIVITY_EVENTS.forEach((ev) => window.addEventListener(ev, bump, { passive: true, capture: true }));
    document.addEventListener('visibilitychange', onVisibility);
    const interval = window.setInterval(check, Math.min(15_000, Math.max(1_000, timeoutMs / 10)));

    return () => {
      ACTIVITY_EVENTS.forEach((ev) => window.removeEventListener(ev, bump, { capture: true }));
      document.removeEventListener('visibilitychange', onVisibility);
      window.clearInterval(interval);
    };
  }, [enabled, timeoutMs]);
}
