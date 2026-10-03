/**
 * Theme switch (Release 3.16): Match Windows / Light / Dark / Warm night. Kept in this browser
 * only (a UI pref, like text size). The sidebar's quick switch and Settings › App both use
 * `useTheme`, so they stay in sync; other FinTrack windows follow through the storage event.
 * Pure parts and the rules: lib/themeCore.ts.
 */
import { useCallback, useEffect, useState } from 'react';
import { readPref, removePref, writePref } from './prefs';
import { applyTheme, isThemeChoice, THEME_KEY, type ThemeChoice } from './themeCore';

const EVENT = 'fintrack:theme';

/** Applies a theme with CSS transitions off for a frame (`.theme-switching`, styles.css). */
function swapTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  root.classList.add('theme-switching');
  applyTheme(choice);
  void root.offsetWidth; // apply the new colors now, while transitions are off
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('theme-switching')));
}

export function readTheme(): ThemeChoice {
  return readPref<ThemeChoice>(THEME_KEY, 'system', isThemeChoice);
}

export function setTheme(choice: ThemeChoice): void {
  // Match Windows is the default: nothing saved.
  if (choice === 'system') removePref(THEME_KEY);
  else writePref(THEME_KEY, choice);
  swapTheme(choice);
  window.dispatchEvent(new Event(EVENT));
}

/** Once, in main.tsx: apply the saved theme (theme-boot.js already did) and follow other windows. */
export function initTheme(): void {
  applyTheme(readTheme());
  window.addEventListener('storage', (e) => {
    if (e.key === null || e.key.endsWith(THEME_KEY)) {
      swapTheme(readTheme());
      window.dispatchEvent(new Event(EVENT));
    }
  });
}

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [theme, setState] = useState<ThemeChoice>(readTheme);
  useEffect(() => {
    const sync = () => setState(readTheme());
    window.addEventListener(EVENT, sync);
    return () => window.removeEventListener(EVENT, sync);
  }, []);
  const set = useCallback((c: ThemeChoice) => {
    setState(c);
    setTheme(c);
  }, []);
  return [theme, set];
}
