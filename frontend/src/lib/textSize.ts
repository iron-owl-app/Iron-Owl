/**
 * Settings › Text size (design D7): Normal / Large / Larger. Scales the root font size
 * (100% / 112% / 125%), so every rem-based size grows: the sidebar, dialogs and the lock
 * screen too. Spacing set in px stays put. Kept in this browser only (a UI pref), applied in
 * main.tsx before the first render so nothing jumps.
 */
import { useCallback, useEffect, useState } from 'react';
import { readPref, writePref } from './prefs';

export type TextSize = 'normal' | 'large' | 'larger';

export interface TextSizeOption {
  id: TextSize;
  label: string;
  /** Root font size. */
  percent: number;
}

export const TEXT_SIZES: TextSizeOption[] = [
  { id: 'normal', label: 'Normal', percent: 100 },
  { id: 'large', label: 'Large', percent: 112 },
  { id: 'larger', label: 'Larger', percent: 125 },
];

const KEY = 'textSize';
const EVENT = 'fintrack:text-size';
const isTextSize = (v: unknown): v is TextSize => v === 'normal' || v === 'large' || v === 'larger';

export function readTextSize(): TextSize {
  return readPref<TextSize>(KEY, 'normal', isTextSize);
}

/** Sets the root font size. Safe to call before React renders. */
export function applyTextSize(size: TextSize): void {
  const opt = TEXT_SIZES.find((o) => o.id === size) ?? TEXT_SIZES[0]!;
  const root = document.documentElement;
  if (opt.percent === 100) root.style.removeProperty('font-size');
  else root.style.fontSize = `${opt.percent}%`;
  root.dataset.textSize = opt.id;
}

export function setTextSize(size: TextSize): void {
  writePref(KEY, size);
  applyTextSize(size);
  window.dispatchEvent(new Event(EVENT));
}

/** Once, in main.tsx: apply the saved size and follow changes made in other FinTrack windows. */
export function initTextSize(): void {
  applyTextSize(readTextSize());
  window.addEventListener('storage', (e) => {
    if (e.key === null || e.key.endsWith(KEY)) {
      applyTextSize(readTextSize());
      window.dispatchEvent(new Event(EVENT));
    }
  });
}

export function useTextSize(): [TextSize, (size: TextSize) => void] {
  const [size, setSize] = useState<TextSize>(readTextSize);
  useEffect(() => {
    const sync = () => setSize(readTextSize());
    window.addEventListener(EVENT, sync);
    return () => window.removeEventListener(EVENT, sync);
  }, []);
  const set = useCallback((s: TextSize) => {
    setSize(s);
    setTextSize(s);
  }, []);
  return [size, set];
}
