/**
 * A small Markdown reader for the Help page (HELP.md at the repo root, imported with `?raw`).
 *
 * It understands only what HELP.md needs: `#`/`##`/`###` headings, paragraphs, `-` and `1.`
 * lists, `**bold**` and `[text](link)`. Everything else is plain text: HTML tags (including
 * `<script>`) come out as the literal characters, because the page renders the result as React
 * text, never as HTML. Links keep only `http(s)://` addresses and in-app `#/` routes; any other
 * link (javascript:, data:, relative paths) is dropped and its words are kept as text.
 */

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'bold'; children: Inline[] }
  | { kind: 'link'; href: string; external: boolean; children: Inline[] };

export type Block =
  | { kind: 'heading'; level: 1 | 2 | 3; id: string; children: Inline[] }
  | { kind: 'paragraph'; children: Inline[] }
  | { kind: 'list'; ordered: boolean; start: number; items: Inline[][] };

const EXTERNAL_RE = /^https?:\/\/[^\s<>"'`]+$/i;
const IN_APP_RE = /^#\/(?!\/)[A-Za-z0-9\-._~/?=&%]*$/;

/** The address if it is one the page may follow, else null. */
export function safeHref(raw: string): { href: string; external: boolean } | null {
  const href = raw.trim();
  if (EXTERNAL_RE.test(href)) return { href, external: true };
  if (IN_APP_RE.test(href)) return { href, external: false };
  return null;
}

/** "Getting your Plaid keys" -> "getting-your-plaid-keys" (heading ids, unique per document). */
export function slug(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'section'
  );
}

/** Plain text of some inline pieces (heading ids, tests). */
export function inlineText(nodes: Inline[]): string {
  return nodes.map((n) => (n.kind === 'text' ? n.text : inlineText(n.children))).join('');
}

/** Merge neighbouring text pieces so the output is easy to compare and render. */
function pushText(out: Inline[], text: string) {
  if (!text) return;
  const last = out[out.length - 1];
  if (last && last.kind === 'text') last.text += text;
  else out.push({ kind: 'text', text });
}

/** `**bold**`, `[text](link)` and backslash escapes; everything else is text. */
export function parseInline(src: string, inLink = false): Inline[] {
  const out: Inline[] = [];
  let i = 0;
  while (i < src.length) {
    const ch = src[i]!;
    if (ch === '\\' && i + 1 < src.length && /[\\*[\]()#\-.!_`]/.test(src[i + 1]!)) {
      pushText(out, src[i + 1]!);
      i += 2;
      continue;
    }
    if (ch === '*' && src.startsWith('**', i)) {
      const end = src.indexOf('**', i + 2);
      if (end > i + 2) {
        out.push({ kind: 'bold', children: parseInline(src.slice(i + 2, end), inLink) });
        i = end + 2;
        continue;
      }
    }
    if (ch === '[' && !inLink) {
      const close = findClose(src, i);
      if (close !== -1 && src[close + 1] === '(') {
        const urlEnd = src.indexOf(')', close + 2);
        if (urlEnd !== -1) {
          const words = parseInline(src.slice(i + 1, close), true);
          const target = safeHref(src.slice(close + 2, urlEnd));
          if (target) out.push({ kind: 'link', href: target.href, external: target.external, children: words });
          else {
            for (const w of words) {
              if (w.kind === 'text') pushText(out, w.text);
              else out.push(w);
            }
          }
          i = urlEnd + 1;
          continue;
        }
      }
    }
    pushText(out, ch);
    i += 1;
  }
  return out;
}

/** The `]` that closes the `[` at `open` (no nesting; escaped `\]` skipped), or -1. */
function findClose(src: string, open: number): number {
  for (let j = open + 1; j < src.length; j++) {
    if (src[j] === '\\') j += 1;
    else if (src[j] === ']') return j;
    else if (src[j] === '[') return -1;
  }
  return -1;
}

const HEADING_RE = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const BULLET_RE = /^\s{0,3}[-*+]\s+(.*)$/;
const NUMBER_RE = /^\s{0,3}(\d{1,3})[.)]\s+(.*)$/;

/** HELP.md text -> blocks. Never throws; unknown syntax stays as words. */
export function parseHelp(markdown: string): Block[] {
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  const ids = new Map<string, number>();
  let para: string[] = [];
  let list: { ordered: boolean; start: number; items: string[] } | null = null;

  const flushPara = () => {
    if (para.length) blocks.push({ kind: 'paragraph', children: parseInline(para.join(' ')) });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push({ kind: 'list', ordered: list.ordered, start: list.start, items: list.items.map((t) => parseInline(t)) });
    list = null;
  };
  const uniqueId = (text: string) => {
    const base = slug(text);
    const n = ids.get(base) ?? 0;
    ids.set(base, n + 1);
    return n ? `${base}-${n + 1}` : base;
  };

  for (const raw of lines) {
    const line = raw.replace(/\t/g, '    ');
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    const h = HEADING_RE.exec(line);
    if (h) {
      flushPara();
      flushList();
      const children = parseInline(h[2]!);
      const level = Math.min(h[1]!.length, 3) as 1 | 2 | 3;
      blocks.push({ kind: 'heading', level, id: uniqueId(inlineText(children)), children });
      continue;
    }
    const b = BULLET_RE.exec(line);
    const n = b ? null : NUMBER_RE.exec(line);
    if (b || n) {
      const ordered = !!n;
      flushPara();
      if (list && list.ordered !== ordered) flushList();
      if (!list) list = { ordered, start: n ? Number(n[1]) : 1, items: [] };
      list.items.push((b ? b[1] : n![2])!.trim());
      continue;
    }
    // An indented line right under a list item continues that item; otherwise it's paragraph text.
    if (list && /^\s{2,}\S/.test(line)) {
      const items: string[] = list.items;
      items[items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushPara();
  flushList();
  return blocks;
}
