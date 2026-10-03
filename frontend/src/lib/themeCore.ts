/**
 * Theme switch (Release 3.16): the pure parts, with no imports so `npm run test:unit` can load
 * them directly. The choice lives in localStorage (lib/prefs.ts key "theme", so the stored key is
 * `fintrack.ui.theme`), and shows as `<html data-theme="light|dark|warm">`; no attribute means
 * Match Windows (the CSS then follows `prefers-color-scheme`). public/theme-boot.js repeats
 * `applyTheme` in plain JS so the saved theme is on before the first paint (the CSP allows no
 * inline script); scripts/theme.test.ts checks the two agree.
 */

export type ThemeChoice = 'system' | 'light' | 'dark' | 'warm';
export type ThemeAttr = Exclude<ThemeChoice, 'system'>;

export interface ThemeOption {
  id: ThemeChoice;
  label: string;
  hint: string;
}

export const THEMES: ThemeOption[] = [
  { id: 'system', label: 'Match Windows', hint: 'Light or dark, the same as Windows.' },
  { id: 'light', label: 'Light', hint: 'Dark words on a light page.' },
  { id: 'dark', label: 'Dark', hint: 'Light words on a dark page.' },
  { id: 'warm', label: 'Warm night', hint: 'Dark, with soft warm colors. Easy on the eyes at night.' },
];

/** The prefs.ts key; the stored key adds the `fintrack.ui.` prefix. */
export const THEME_KEY = 'theme';
export const THEME_STORAGE_KEY = `fintrack.ui.${THEME_KEY}`;

/** The window's title bar color (`<meta name="theme-color">`): each theme's page background. */
export const THEME_COLORS: Record<ThemeAttr, string> = {
  light: '#f7f6f2',
  dark: '#0b0e14',
  warm: '#140e09',
};

export function isThemeChoice(v: unknown): v is ThemeChoice {
  return v === 'system' || v === 'light' || v === 'dark' || v === 'warm';
}

/** What localStorage holds (JSON, as prefs.ts writes it) -> the choice; anything odd is Match Windows. */
export function parseStoredTheme(raw: string | null): ThemeChoice {
  if (raw === null) return 'system';
  try {
    const v: unknown = JSON.parse(raw);
    return isThemeChoice(v) ? v : 'system';
  } catch {
    return 'system';
  }
}

/** The `data-theme` value, or null for Match Windows (no attribute). */
export function themeAttr(choice: ThemeChoice): ThemeAttr | null {
  return choice === 'system' ? null : choice;
}

export function themeLabel(choice: ThemeChoice): string {
  return (THEMES.find((t) => t.id === choice) ?? THEMES[0]!).label;
}

interface AttrNode {
  getAttribute(name: string): string | null;
  setAttribute(name: string, value: string): void;
  removeAttribute(name: string): void;
}

/** The bits of `document` applyTheme touches (tests pass a fake). */
export interface ThemeDoc {
  documentElement: AttrNode;
  querySelectorAll(selector: string): ArrayLike<AttrNode>;
}

/**
 * Sets (or, for Match Windows, removes) `data-theme` on <html> and points the title bar
 * color metas at the theme. Safe to call before React renders.
 */
export function applyTheme(choice: ThemeChoice, doc: ThemeDoc = document): void {
  const attr = themeAttr(choice);
  if (attr) doc.documentElement.setAttribute('data-theme', attr);
  else doc.documentElement.removeAttribute('data-theme');
  const metas = doc.querySelectorAll('meta[name="theme-color"]');
  for (let i = 0; i < metas.length; i++) {
    const meta = metas[i]!;
    // Match Windows: each meta keeps its own scheme (index.html has one per media query).
    const own = /dark/.test(meta.getAttribute('media') ?? '') ? 'dark' : 'light';
    meta.setAttribute('content', THEME_COLORS[attr ?? own]);
  }
}

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/**
 * Where the sidebar's Colors panel goes (fixed position): just right of the button, its bottom
 * level with the button's bottom, then moved up/left as needed to stay `margin` inside the
 * window (and never above its top). The button itself never moves.
 */
export function popoverPlace(
  button: Box,
  size: { width: number; height: number },
  view: { width: number; height: number },
  gap = 8,
  margin = 8,
): { left: number; top: number } {
  let left = button.right + gap;
  if (left + size.width > view.width - margin) left = Math.max(margin, view.width - margin - size.width);
  let top = button.bottom - size.height;
  if (top + size.height > view.height - margin) top = view.height - margin - size.height;
  top = Math.max(margin, top);
  return { left: Math.round(left), top: Math.round(top) };
}
