/**
 * Unit tests for the Help page's Markdown reader (src/lib/helpMarkdown.ts) and HELP.md itself.
 * Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { inlineText, parseHelp, parseInline, safeHref, type Block, type Inline } from '../src/lib/helpMarkdown.ts';

const text = (t: string): Inline => ({ kind: 'text', text: t });

test('headings, paragraphs and lists', () => {
  const blocks = parseHelp('# Help\n\nFirst line\nsecond line.\n\n## Two\n- one\n- two\n  more\n\n1. a\n2. b\n');
  assert.deepEqual(blocks, [
    { kind: 'heading', level: 1, id: 'help', children: [text('Help')] },
    { kind: 'paragraph', children: [text('First line second line.')] },
    { kind: 'heading', level: 2, id: 'two', children: [text('Two')] },
    { kind: 'list', ordered: false, start: 1, items: [[text('one')], [text('two more')]] },
    { kind: 'list', ordered: true, start: 1, items: [[text('a')], [text('b')]] },
  ]);
});

test('a numbered list keeps its first number; #### becomes a level 3 heading', () => {
  const blocks = parseHelp('3. three\n4. four\n\n#### Small');
  assert.equal(blocks[0]!.kind === 'list' && blocks[0].start, 3);
  assert.equal(blocks[1]!.kind === 'heading' && blocks[1].level, 3);
});

test('repeated headings get unique ids', () => {
  const ids = parseHelp('## Backups\n## Backups\n## Backups').map((b) => (b.kind === 'heading' ? b.id : ''));
  assert.deepEqual(ids, ['backups', 'backups-2', 'backups-3']);
});

test('bold and links', () => {
  assert.deepEqual(parseInline('Click **More info**, then [Plaid](https://plaid.com).'), [
    text('Click '),
    { kind: 'bold', children: [text('More info')] },
    text(', then '),
    { kind: 'link', href: 'https://plaid.com', external: true, children: [text('Plaid')] },
    text('.'),
  ]);
  assert.deepEqual(parseInline('[Settings](#/settings/banks)'), [
    { kind: 'link', href: '#/settings/banks', external: false, children: [text('Settings')] },
  ]);
});

test('unsafe links keep their words but lose the link', () => {
  for (const href of ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', 'data:text/html,hi', 'vbscript:x', '//evil.example', '#//evil.example', 'file:///C:/x', 'settings', '#top', 'http://a b']) {
    assert.equal(safeHref(href), null, href);
    assert.deepEqual(parseInline(`[click](${href})`).filter((n) => n.kind === 'link'), [], href);
  }
  assert.equal(inlineText(parseInline('[click](javascript:alert(1))')), 'click)');
});

test('HTML and <script> come out as plain text', () => {
  const src = '<script>alert("x")</script>\n\n<img src=x onerror=alert(1)> **<b>hi</b>**';
  const blocks = parseHelp(src);
  assert.deepEqual(blocks[0], { kind: 'paragraph', children: [text('<script>alert("x")</script>')] });
  assert.deepEqual(blocks[1], {
    kind: 'paragraph',
    children: [text('<img src=x onerror=alert(1)> '), { kind: 'bold', children: [text('<b>hi</b>')] }],
  });
});

test('unclosed markers and backslash escapes stay as text', () => {
  assert.deepEqual(parseInline('a ** b [c] (d) \\*\\*e\\*\\*'), [text('a ** b [c] (d) **e**')]);
  assert.deepEqual(parseHelp(''), []);
  assert.deepEqual(parseHelp('\r\n\r\n'), []);
});

test('HELP.md parses, has the sections the page needs, and only safe links', () => {
  const blocks: Block[] = parseHelp(readFileSync(new URL('../../HELP.md', import.meta.url), 'utf8'));
  const headings = blocks.filter((b) => b.kind === 'heading').map((b) => inlineText(b.children));
  assert.equal(headings[0], 'Iron Owl Help');
  const all = JSON.stringify(blocks);
  assert.match(all, /Your data stays on this PC/);
  assert.match(all, /More info/);
  assert.match(all, /Run anyway/);
  // Every link that survived parsing is http(s) or in-app; and no link in the file was dropped.
  const raw = readFileSync(new URL('../../HELP.md', import.meta.url), 'utf8');
  const written = [...raw.matchAll(/\]\(([^)]*)\)/g)].map((m) => m[1]!);
  for (const href of written) assert.ok(safeHref(href), `HELP.md link not allowed: ${href}`);
  assert.doesNotMatch(raw, /FinTrack/);
});
