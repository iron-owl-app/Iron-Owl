/**
 * Unit tests for the theme switch (Release 3.16): the pure choice/attribute logic
 * (src/lib/themeCore.ts), the pre-paint script (public/theme-boot.js) agreeing with it, every
 * dark CSS block also answering to data-theme, and Warm night's contrast. Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {
  applyTheme,
  isThemeChoice,
  parseStoredTheme,
  popoverPlace,
  THEME_COLORS,
  THEME_STORAGE_KEY,
  themeAttr,
  themeLabel,
  THEMES,
  type ThemeChoice,
  type ThemeDoc,
} from '../src/lib/themeCore.ts';

const FRONTEND = path.resolve(import.meta.dirname, '..');

class FakeNode {
  attrs = new Map<string, string>();
  constructor(init: Record<string, string> = {}) {
    for (const [k, v] of Object.entries(init)) this.attrs.set(k, v);
  }
  getAttribute(n: string) {
    return this.attrs.get(n) ?? null;
  }
  setAttribute(n: string, v: string) {
    this.attrs.set(n, v);
  }
  removeAttribute(n: string) {
    this.attrs.delete(n);
  }
}

function fakeDoc(theme?: string) {
  const html = new FakeNode(theme ? { 'data-theme': theme } : {});
  const metas = [
    new FakeNode({ name: 'theme-color', media: '(prefers-color-scheme: light)', content: '#f7f6f2' }),
    new FakeNode({ name: 'theme-color', media: '(prefers-color-scheme: dark)', content: '#0b0e14' }),
  ];
  const doc = {
    documentElement: html,
    querySelectorAll: (sel: string) => (sel === 'meta[name="theme-color"]' ? metas : []),
  } satisfies ThemeDoc;
  return { doc, html, metas };
}

test('the four choices, in order, with plain labels', () => {
  assert.deepEqual(
    THEMES.map((t) => [t.id, t.label]),
    [
      ['system', 'Match Windows'],
      ['light', 'Light'],
      ['dark', 'Dark'],
      ['warm', 'Warm night'],
    ],
  );
  assert.equal(themeLabel('warm'), 'Warm night');
  assert.equal(THEME_STORAGE_KEY, 'fintrack.ui.theme');
});

test('isThemeChoice accepts only the four ids', () => {
  for (const t of THEMES) assert.ok(isThemeChoice(t.id));
  for (const v of ['', 'Dark', 'night', null, undefined, 1, {}]) assert.equal(isThemeChoice(v), false);
});

test('nothing saved, or anything odd, is Match Windows', () => {
  assert.equal(parseStoredTheme(null), 'system');
  assert.equal(parseStoredTheme('not json'), 'system');
  assert.equal(parseStoredTheme('"purple"'), 'system');
  assert.equal(parseStoredTheme('42'), 'system');
  assert.equal(parseStoredTheme('"warm"'), 'warm');
  assert.equal(parseStoredTheme('"light"'), 'light');
});

test('themeAttr: Match Windows has no attribute', () => {
  assert.equal(themeAttr('system'), null);
  assert.equal(themeAttr('light'), 'light');
  assert.equal(themeAttr('dark'), 'dark');
  assert.equal(themeAttr('warm'), 'warm');
});

test('applyTheme sets the attribute and both title bar colors', () => {
  const { doc, html, metas } = fakeDoc();
  applyTheme('warm', doc);
  assert.equal(html.getAttribute('data-theme'), 'warm');
  assert.deepEqual(metas.map((m) => m.getAttribute('content')), [THEME_COLORS.warm, THEME_COLORS.warm]);
  applyTheme('light', doc);
  assert.equal(html.getAttribute('data-theme'), 'light');
  assert.deepEqual(metas.map((m) => m.getAttribute('content')), [THEME_COLORS.light, THEME_COLORS.light]);
});

test('applyTheme(Match Windows) removes the attribute and restores each meta', () => {
  const { doc, html, metas } = fakeDoc('dark');
  applyTheme('dark', doc);
  applyTheme('system', doc);
  assert.equal(html.getAttribute('data-theme'), null);
  assert.deepEqual(metas.map((m) => m.getAttribute('content')), [THEME_COLORS.light, THEME_COLORS.dark]);
});

test('the colors index.html starts with are the Match Windows ones', () => {
  const html = fs.readFileSync(path.join(FRONTEND, 'index.html'), 'utf8');
  assert.match(html, new RegExp(`media="\\(prefers-color-scheme: light\\)" content="${THEME_COLORS.light}"`));
  assert.match(html, new RegExp(`media="\\(prefers-color-scheme: dark\\)" content="${THEME_COLORS.dark}"`));
  // The pre-paint script is a same-origin file (the CSP blocks inline scripts), loaded before the boot styles.
  const script = html.indexOf('<script src="/theme-boot.js"></script>');
  assert.ok(script > 0 && script < html.indexOf('boot.css'));
});

test('public/theme-boot.js does what applyTheme does, for every stored value', () => {
  const code = fs.readFileSync(path.join(FRONTEND, 'public', 'theme-boot.js'), 'utf8');
  const stored: (string | null)[] = [null, '"system"', '"light"', '"dark"', '"warm"', '"neon"', '{bad', '7'];
  for (const raw of stored) {
    const viaScript = fakeDoc();
    const storage = { getItem: (k: string) => (k === THEME_STORAGE_KEY ? raw : null) };
    vm.runInNewContext(code, { window: { localStorage: storage }, document: viaScript.doc });
    const viaCore = fakeDoc();
    applyTheme(parseStoredTheme(raw), viaCore.doc);
    assert.equal(viaScript.html.getAttribute('data-theme'), viaCore.html.getAttribute('data-theme'), `stored ${raw}`);
    assert.deepEqual(
      viaScript.metas.map((m) => m.getAttribute('content')),
      viaCore.metas.map((m) => m.getAttribute('content')),
      `stored ${raw}`,
    );
  }
  // Storage that throws (blocked site data): Match Windows, no error.
  const blocked = fakeDoc();
  const throwing = {
    get localStorage(): never {
      throw new Error('blocked');
    },
  };
  vm.runInNewContext(code, { window: throwing, document: blocked.doc });
  assert.equal(blocked.html.getAttribute('data-theme'), null);
});

test('the sidebar Colors panel opens right of the button and stays inside the window', () => {
  const size = { width: 240, height: 230 };
  // Sidebar button near the bottom at 1366x768 and 1280x720: bottom-aligned with it, fully visible.
  for (const view of [{ width: 1366, height: 768 }, { width: 1280, height: 720 }]) {
    const button = { left: 12, right: 220, top: view.height - 60, bottom: view.height - 16 };
    const p = popoverPlace(button, size, view);
    assert.equal(p.left, 228);
    assert.equal(p.top + size.height, button.bottom);
    assert.ok(p.top >= 8 && p.top + size.height <= view.height - 8);
  }
  // A button below the window's bottom (scrolled sidebar) or a short window: kept inside.
  const low = popoverPlace({ left: 12, right: 220, top: 900, bottom: 944 }, size, { width: 1280, height: 720 });
  assert.equal(low.top, 720 - 8 - 230);
  const short = popoverPlace({ left: 12, right: 220, top: 100, bottom: 144 }, size, { width: 1280, height: 200 });
  assert.equal(short.top, 8);
  // A narrow window: moved left to fit.
  assert.equal(popoverPlace({ left: 12, right: 220, top: 600, bottom: 644 }, size, { width: 400, height: 720 }).left, 400 - 8 - 240);
});

// ------------------------------------------------------------------ CSS

function cssFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) return cssFiles(p);
    return e.name.endsWith('.css') ? [p] : [];
  });
}

/** The `{...}` body starting at `open` (index of the brace). */
function blockAt(css: string, open: number): string {
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}' && --depth === 0) return css.slice(open + 1, i);
  }
  throw new Error('unbalanced braces');
}

/** Selectors of the rules directly inside a block body. */
function selectors(body: string): string[] {
  const out: string[] = [];
  const clean = body.replace(/\/\*[\s\S]*?\*\//g, '');
  let depth = 0;
  let start = 0;
  for (let i = 0; i < clean.length; i++) {
    if (clean[i] === '{') {
      if (depth === 0) out.push(clean.slice(start, i).trim());
      depth++;
    } else if (clean[i] === '}') {
      depth--;
      if (depth === 0) start = i + 1;
    }
  }
  return out;
}

const norm = (s: string) => s.replace(/\s+/g, ' ').trim();

test('every Windows-dark block applies only with no theme chosen, and has a Dark / Warm night twin', () => {
  const files = [...cssFiles(path.join(FRONTEND, 'src')), path.join(FRONTEND, 'public', 'boot.css')];
  let blocks = 0;
  for (const file of files) {
    const rel = path.relative(FRONTEND, file);
    const css = fs.readFileSync(file, 'utf8');
    const flat = norm(css);
    const re = /@media \(prefers-color-scheme: dark\) \{/g;
    for (let m = re.exec(css); m; m = re.exec(css)) {
      blocks++;
      const body = blockAt(css, m.index + m[0].length - 1);
      for (const sel of selectors(body)) {
        for (const one of sel.split(',')) {
          assert.match(one, /:not\(\[data-theme\]\)/, `${rel}: "${one.trim()}" inside the dark media block must be limited to :not([data-theme])`);
        }
        const twin = sel
          .replace(/:root:not\(\[data-theme\]\)/g, ':root[data-theme="dark"], :root[data-theme="warm"]')
          .replace(/:not\(\[data-theme\]\)/g, '[data-theme="dark"], [data-theme="warm"]');
        assert.ok(flat.includes(`${norm(twin)} {`), `${rel}: no Dark / Warm night twin for "${sel}"`);
      }
    }
    assert.doesNotMatch(css, /prefers-color-scheme:\s*light/, `${rel}: light media blocks would ignore the Light / Dark choice`);
  }
  assert.ok(blocks >= 20, `found only ${blocks} dark blocks`);
});

// ------------------------------------------------------------------ Warm night contrast

function oklchToLinear(L: number, C: number, H: number): [number, number, number] {
  const h = (H * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const c = (x: number) => Math.min(1, Math.max(0, x));
  return [
    c(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    c(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    c(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ];
}
function contrast(a: [number, number, number], b: [number, number, number]): number {
  const y = (v: [number, number, number]) => 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  const [hi, lo] = [Math.max(y(a), y(b)), Math.min(y(a), y(b))];
  return (hi + 0.05) / (lo + 0.05);
}

test('Warm night: body text 4.5:1, borders and warnings 3:1, and no red', () => {
  const css = fs.readFileSync(path.join(FRONTEND, 'src', 'styles.css'), 'utf8');
  const at = css.indexOf(':root:where([data-theme="warm"]) {');
  assert.ok(at > css.indexOf(':root:where([data-theme="dark"], [data-theme="warm"]) {'), 'the warm block comes after the dark one');
  const body = blockAt(css, css.indexOf('{', at));
  const tok = new Map<string, [number, number, number]>();
  const hue = new Map<string, number>();
  for (const m of body.matchAll(/--([\w-]+):\s*oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)/g)) {
    tok.set(m[1]!, oklchToLinear(Number(m[2]), Number(m[3]), Number(m[4])));
    hue.set(m[1]!, Number(m[4]));
  }
  const pairs: [string, string, number][] = [
    ['text', 'bg', 4.5],
    ['text', 'surface', 4.5],
    ['text', 'surface-3', 4.5],
    ['text-2', 'surface', 4.5],
    ['text-3', 'surface', 4.5],
    ['text-3', 'bg', 4.5],
    ['accent-fg', 'accent', 4.5],
    ['accent-fg', 'accent-hover', 4.5],
    ['accent-ink', 'surface', 4.5],
    ['accent-ink', 'accent-soft', 4.5],
    ['text', 'accent-soft', 4.5],
    ['warn', 'surface', 4.5],
    ['warn', 'warn-soft', 4.5],
    ['text', 'warn-soft', 4.5],
    ['warn-ink', 'warn', 4.5],
    ['focus', 'surface', 3],
    ['border-strong', 'surface', 3],
    ['border-strong', 'bg', 3],
    ['warn-border', 'surface', 3],
    ['amber-edge', 'surface', 3],
    ['amber-edge', 'bg', 3],
    ['toast-accent-ink', 'toast-accent', 4.5],
  ];
  for (const [fg, bg, min] of pairs) {
    assert.ok(tok.has(fg) && tok.has(bg), `missing --${fg} or --${bg}`);
    const r = contrast(tok.get(fg)!, tok.get(bg)!);
    assert.ok(r >= min, `--${fg} on --${bg}: ${r.toFixed(2)} < ${min}`);
  }
  // Warm hues only (no red, low blue): 40-100 for every color token in the block.
  for (const [k, h] of hue) assert.ok(h >= 40 && h <= 100, `--${k} hue ${h}`);
  // "Needs you" stays apart from the copper accent.
  assert.ok(hue.get('warn')! - hue.get('accent')! >= 30, 'warning and accent hues too close');
});
