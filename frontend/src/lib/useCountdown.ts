import { useCallback, useEffect, useState } from 'react';

/** Seconds remaining until a 429 lockout expires; `start(n)` begins a new one. */
export function useCountdown(onDone?: () => void): [number, (seconds: number) => void] {
  const [until, setUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (until === null) return;
    const h = window.setInterval(() => {
      const t = Date.now();
      setNow(t);
      if (t >= until) {
        setUntil(null);
        onDone?.();
      }
    }, 250);
    return () => window.clearInterval(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [until]);

  const start = useCallback((seconds: number) => {
    const t = Date.now();
    setNow(t);
    setUntil(t + Math.max(1, seconds) * 1000);
  }, []);

  return [until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000)), start];
}
