import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import type { CategoryGroup, CategoryKind, CategorySource, TxnCategory } from '../api';
import { categoryTone, groupHueAt, groupIndexMap, toneStyle, type CategoryTone } from '../lib/categoryColors';
import { Icon } from './Icon';

const KIND_LABEL: Record<CategoryKind, string> = { spending: 'Spending', income: 'Income', transfer: 'Transfers', fixed: 'Fixed costs' };
const KIND_ORDER: CategoryKind[] = ['spending', 'income', 'transfer', 'fixed'];
const WIDTH = 300;
const MAX_HEIGHT = 420;
const GAP = 6;
const EDGE = 8;

/** Small color dot for a transaction category (design's segment color, oklch(0.62 0.12 H)). */
export function CategoryDot({ hue }: { hue: number }) {
  return <span className="cat-dot" style={{ '--h': hue } as CSSProperties} aria-hidden="true" />;
}

/** Hand-set sources: the category was picked by the user (one or several at once). */
export const isHandSet = (s: CategorySource | null | undefined) => s === 'user' || s === 'user_bulk';

type Option = { id: string | null; name: string; tone: CategoryTone | null };
type Section = { key: string; label: string | null; tone: CategoryTone | null; layout: 'reset' | 'chips' | 'rows'; items: number[] };

export type PopoverCloseReason = 'escape' | 'outside' | 'tab' | 'viewport';

/**
 * Searchable category picker anchored to a rectangle (combobox pattern): type to filter,
 * ↑/↓/Home/End to move, Enter to pick (with an empty search, the first recently used
 * category), Esc to close. Sections: "Reset to automatic" (hand-set only), recently used
 * chips, budget groups in position order, then ungrouped categories by kind.
 * The parent owns focus: it restores focus to the trigger after a pick or Esc.
 */
export function CategoryPopover({
  anchor,
  current,
  source,
  categories,
  groups,
  recent,
  bulkCount,
  onPick,
  onClose,
}: {
  anchor: DOMRect;
  /** Current category id, or null (none / several). */
  current: string | null;
  source: CategorySource | null;
  categories: TxnCategory[];
  groups: CategoryGroup[];
  /** Recently used category ids, most recent first. */
  recent: string[];
  /** Bulk mode: "Set a category for N transactions". */
  bulkCount?: number;
  onPick: (id: string | null) => void;
  onClose: (reason: PopoverCloseReason) => void;
}) {
  const uid = useId();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const { options, sections, first } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const opts: Option[] = [];
    const secs: Section[] = [];
    const push = (sec: Section, o: Option) => {
      sec.items.push(opts.length);
      opts.push(o);
    };
    const gIndex = groupIndexMap(groups);
    const tone = (c: TxnCategory) => categoryTone(c, c.group_id === null ? null : gIndex.get(c.group_id));
    const shown = (c: TxnCategory) => !c.hidden || c.id === current;
    const byId = new Map(categories.map((c) => [c.id, c]));

    if (!q && isHandSet(source)) {
      const sec: Section = { key: 'reset', label: null, tone: null, layout: 'reset', items: [] };
      push(sec, { id: null, name: 'Reset to automatic', tone: null });
      secs.push(sec);
    }
    let recentStart = -1;
    if (!q) {
      const sec: Section = { key: 'recent', label: 'Recently used', tone: null, layout: 'chips', items: [] };
      for (const id of recent) {
        const c = byId.get(id);
        if (c && !c.hidden) push(sec, { id: c.id, name: c.name, tone: tone(c) });
      }
      if (sec.items.length) {
        recentStart = sec.items[0]!;
        secs.push(sec);
      }
    }
    const matches = (c: TxnCategory, label: string) => !q || c.name.toLowerCase().includes(q) || label.toLowerCase().includes(q);
    const inGroup = new Set<string>();
    [...groups]
      .sort((a, b) => a.position - b.position || a.id - b.id)
      .forEach((g, i) => {
        // The group's own order, then any categories pointing at it that the list missed.
        const ids = [...g.category_ids, ...categories.filter((c) => c.group_id === g.id && !g.category_ids.includes(c.id)).map((c) => c.id)];
        const cats = ids.map((id) => byId.get(id)).filter((c): c is TxnCategory => !!c);
        cats.forEach((c) => inGroup.add(c.id));
        const allTransfers = cats.length > 0 && cats.every((c) => c.kind === 'transfer');
        const sec: Section = { key: `g${g.id}`, label: g.name, tone: { hue: groupHueAt(i), k: allTransfers ? 0.015 : 1 }, layout: 'rows', items: [] };
        for (const c of cats) if (shown(c) && matches(c, g.name)) push(sec, { id: c.id, name: c.name, tone: tone(c) });
        if (sec.items.length) secs.push(sec);
      });
    for (const k of KIND_ORDER) {
      const sec: Section = { key: `k${k}`, label: KIND_LABEL[k], tone: null, layout: 'rows', items: [] };
      for (const c of categories) {
        if (c.kind !== k || inGroup.has(c.id) || !shown(c)) continue;
        if (matches(c, KIND_LABEL[k])) push(sec, { id: c.id, name: c.name, tone: tone(c) });
      }
      if (sec.items.length) secs.push(sec);
    }
    // The highlight starts on what Enter picks: the first recent chip, else the current category.
    const cur = opts.findIndex((o) => o.id !== null && o.id === current);
    const start = q ? 0 : recentStart >= 0 ? recentStart : cur >= 0 ? cur : 0;
    return { options: opts, sections: secs, first: start };
  }, [categories, groups, recent, query, source, current]);

  // A new option list (the search changed): back to the first pick.
  useEffect(() => setActive(first), [first, options]);

  // Focus the search once placed (it renders hidden until measured, and hidden inputs can't take focus).
  const placed = pos !== null;
  useLayoutEffect(() => {
    if (placed) inputRef.current?.focus({ preventScroll: true });
  }, [placed]);

  // Below the anchor; above it when there's no room below; always inside the viewport.
  useLayoutEffect(() => {
    const h = popRef.current?.offsetHeight ?? MAX_HEIGHT;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const w = Math.min(WIDTH, vw - 2 * EDGE);
    const left = Math.max(EDGE, Math.min(anchor.left, vw - w - EDGE));
    const below = anchor.bottom + GAP;
    const fitsAbove = anchor.top - GAP - h >= EDGE;
    const top = below + h + EDGE > vh && fitsAbove ? anchor.top - GAP - h : Math.max(EDGE, Math.min(below, vh - h - EDGE));
    setPos((p) => (p && p.top === top && p.left === left ? p : { top, left }));
  }, [anchor, options.length]);

  // Keep the active option in view (inside the popover's own list only).
  useEffect(() => {
    const list = listRef.current;
    const el = list?.querySelector<HTMLElement>(`[data-i="${active}"]`);
    if (!list || !el) return;
    const lr = list.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    if (er.top < lr.top) list.scrollTop -= lr.top - er.top + 4;
    else if (er.bottom > lr.bottom) list.scrollTop += er.bottom - lr.bottom + 4;
  }, [active]);

  // Close on outside pointerdown, resize, or scrolling anything but the popover.
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (!popRef.current?.contains(e.target as Node)) onCloseRef.current('outside');
    };
    const onScroll = (e: Event) => {
      if (!popRef.current?.contains(e.target as Node)) onCloseRef.current('viewport');
    };
    const onResize = () => onCloseRef.current('viewport');
    document.addEventListener('pointerdown', onDown, true);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onResize);
    return () => {
      document.removeEventListener('pointerdown', onDown, true);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onResize);
    };
  }, []);

  function choose(o: Option | undefined) {
    if (o) onPick(o.id);
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    // The page listens for shortcuts on window; nothing typed here should reach it.
    e.stopPropagation();
    const last = options.length - 1;
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Home' || e.key === 'End') && last < 0) {
      e.preventDefault();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => Math.min(last, a + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(0, Math.min(last, a - 1)));
    } else if (e.key === 'Home') {
      e.preventDefault();
      setActive(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      setActive(last);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      choose(options[active]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose('escape');
    } else if (e.key === 'Tab') {
      onClose('tab');
    }
  }

  const listId = `${uid}-list`;
  const optId = (i: number) => `${uid}-o${i}`;
  const optProps = (i: number) => ({
    id: optId(i),
    'data-i': i,
    role: 'option' as const,
    'aria-selected': options[i]!.id !== null && options[i]!.id === current,
    onPointerMove: () => setActive(i),
    // Keep focus in the search box so the combobox stays in charge.
    onPointerDown: (e: { preventDefault: () => void }) => e.preventDefault(),
    onClick: () => choose(options[i]),
  });

  return createPortal(
    <div
      ref={popRef}
      className="cat-pop"
      style={{ top: pos?.top ?? anchor.bottom + GAP, left: pos?.left ?? anchor.left, visibility: pos ? undefined : 'hidden' }}
      role="dialog"
      aria-label={bulkCount !== undefined ? `Set a category for ${bulkCount} transactions` : 'Choose a category'}
    >
      {bulkCount !== undefined && (
        <div className="cat-pop-title">
          Set a category for {bulkCount.toLocaleString()} transaction{bulkCount === 1 ? '' : 's'}
        </div>
      )}
      <div className="cat-pop-search">
        <Icon name="search" />
        <input
          ref={inputRef}
          className="cat-pop-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKey}
          placeholder="Search categories"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={options[active] ? optId(active) : undefined}
          aria-autocomplete="list"
          aria-label="Search categories"
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      <div ref={listRef} id={listId} className="cat-pop-list" role="listbox" aria-label="Categories">
        {options.length === 0 && <div className="cat-pop-empty">No category matches.</div>}
        {sections.map((sec) =>
          sec.layout === 'reset' ? (
            <div key={sec.key} role="presentation">
              {sec.items.map((i) => (
                <div key={i} {...optProps(i)} className={`cat-pop-opt is-reset${i === active ? ' is-active' : ''}`}>
                  <Icon name="sync" />
                  <span className="truncate">{options[i]!.name}</span>
                </div>
              ))}
            </div>
          ) : (
            <div key={sec.key} role="group" aria-label={sec.label ?? undefined}>
              <div className={`cat-pop-group${sec.tone ? ' is-toned' : ''}`} style={sec.tone ? toneStyle(sec.tone) : undefined} aria-hidden="true">
                {sec.tone && <span className="cat-pop-square" />}
                {sec.label}
              </div>
              {sec.layout === 'chips' ? (
                <div className="cat-pop-chips" role="presentation">
                  {sec.items.map((i) => (
                    <div key={i} {...optProps(i)} className={`cat-pop-chip${i === active ? ' is-active' : ''}`} style={toneStyle(options[i]!.tone!)}>
                      <span className="cat-pop-chip-dot" aria-hidden="true" />
                      <span className="truncate">{options[i]!.name}</span>
                    </div>
                  ))}
                </div>
              ) : (
                sec.items.map((i) => {
                  const on = options[i]!.id !== null && options[i]!.id === current;
                  return (
                    <div key={i} {...optProps(i)} className={`cat-pop-opt${i === active ? ' is-active' : ''}${on ? ' is-current' : ''}`}>
                      <span className="truncate">{options[i]!.name}</span>
                      {on && <Icon name="check" className="cat-pop-check" />}
                    </div>
                  );
                })
              )}
            </div>
          ),
        )}
      </div>
    </div>,
    document.body,
  );
}
