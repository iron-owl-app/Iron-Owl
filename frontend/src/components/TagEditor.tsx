import { useEffect, useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import { api, errorMessage, type Tag, type TagRef, type Transaction } from '../api';
import { Icon } from './Icon';
import { useToast } from './Toast';

const MAX_TAGS = 20;

/** Small colored tag label (hue from the tag). */
export function TagChip({ tag, onRemove, disabled }: { tag: TagRef; onRemove?: () => void; disabled?: boolean }) {
  return (
    <span className={`tag-chip${onRemove ? ' has-remove' : ''}`} style={{ '--h': tag.hue } as CSSProperties}>
      <Icon name="tag" className="tag-chip-icon" />
      <span className="truncate">{tag.name}</span>
      {onRemove && (
        <button type="button" className="tag-chip-x" onClick={onRemove} disabled={disabled} aria-label={`Remove tag ${tag.name}`}>
          <Icon name="x" />
        </button>
      )}
    </span>
  );
}

/** Up to `max` chips, then "+N". For table rows. */
export function TagChips({ tags, max = 3 }: { tags: TagRef[]; max?: number }) {
  if (!tags.length) return null;
  const shown = tags.slice(0, max);
  const more = tags.length - shown.length;
  return (
    <span className="tag-chips">
      <span className="sr-only">Tags: </span>
      {shown.map((t) => (
        <TagChip key={t.id} tag={t} />
      ))}
      {more > 0 && (
        <span className="tag-more" title={tags.slice(max).map((t) => t.name).join(', ')}>
          +{more}
          <span className="sr-only"> more: {tags.slice(max).map((t) => t.name).join(', ')}</span>
        </span>
      )}
    </span>
  );
}

type Option = { kind: 'tag'; tag: Tag } | { kind: 'create'; name: string };

/**
 * Multi-select tag editor for one transaction: chips with remove buttons, and a
 * combobox that filters existing tags (Enter toggles) or creates a new one inline.
 * Every change saves right away.
 */
export function TagEditor({
  txn,
  allTags,
  onUpdated,
  onTagCreated,
}: {
  txn: Transaction;
  allTags: Tag[];
  onUpdated: (t: Transaction) => void;
  onTagCreated: (t: Tag) => void;
}) {
  const uid = useId();
  const toast = useToast();
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>('');
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const selected = useMemo(() => new Set(txn.tags.map((t) => t.id)), [txn.tags]);
  const full = txn.tags.length >= MAX_TAGS;

  const options = useMemo<Option[]>(() => {
    // Normalized like the server (collapsed spaces, case-insensitive), so "Summer  trip"
    // matches an existing "Summer trip" instead of offering a create that would be a 409.
    const name = query.trim().replace(/\s+/g, ' ').slice(0, 40).trim();
    const q = name.toLowerCase();
    const out: Option[] = allTags
      .filter((t) => !q || t.name.toLowerCase().includes(q))
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((tag) => ({ kind: 'tag' as const, tag }));
    const exact = allTags.some((t) => t.name.toLowerCase() === q);
    if (q && !exact) out.push({ kind: 'create', name });
    return out;
  }, [allTags, query]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [active, open]);

  async function save(ids: number[], message: string) {
    setBusy(true);
    setStatus('Saving…');
    try {
      const u = await api.tags.setOnTransaction(txn.id, ids);
      onUpdated(u);
      setStatus(message);
    } catch (e) {
      setStatus('Not saved');
      toast.push({ tone: 'error', title: 'Couldn’t update tags', body: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  }

  async function choose(o: Option | undefined) {
    if (!o || busy) return;
    if (o.kind === 'tag') {
      const on = selected.has(o.tag.id);
      if (!on && full) return setStatus(`A transaction can have up to ${MAX_TAGS} tags.`);
      const ids = on ? txn.tags.map((t) => t.id).filter((id) => id !== o.tag.id) : [...txn.tags.map((t) => t.id), o.tag.id];
      setQuery('');
      await save(ids, on ? `Removed ${o.tag.name}` : `Added ${o.tag.name}`);
      return;
    }
    if (full) return setStatus(`A transaction can have up to ${MAX_TAGS} tags.`);
    setBusy(true);
    setStatus('Creating tag…');
    try {
      const tag = await api.tags.create(o.name);
      onTagCreated(tag);
      setQuery('');
      setBusy(false);
      await save([...txn.tags.map((t) => t.id), tag.id], `Created and added ${tag.name}`);
    } catch (e) {
      setBusy(false);
      setStatus('Not saved');
      toast.push({ tone: 'error', title: 'Couldn’t create the tag', body: errorMessage(e) });
    }
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(options.length - 1, a + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (open) void choose(options[active]);
      else setOpen(true);
    } else if (e.key === 'Escape') {
      if (open || query) {
        // Close the list first; a second Esc can close whatever is around us.
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        setQuery('');
      }
    } else if (e.key === 'Backspace' && !query && txn.tags.length) {
      const last = txn.tags[txn.tags.length - 1]!;
      void save(
        txn.tags.slice(0, -1).map((t) => t.id),
        `Removed ${last.name}`,
      );
    }
  }

  const listId = `${uid}-list`;
  const optId = (i: number) => `${uid}-o${i}`;

  return (
    <div className="field tag-field">
      <label className="field-label" htmlFor={`${uid}-input`}>
        Tags
      </label>
      <div className={`tag-box${open ? ' is-open' : ''}`} onClick={() => inputRef.current?.focus()}>
        {txn.tags.map((t) => (
          <TagChip
            key={t.id}
            tag={t}
            disabled={busy}
            onRemove={() =>
              void save(
                txn.tags.filter((x) => x.id !== t.id).map((x) => x.id),
                `Removed ${t.name}`,
              )
            }
          />
        ))}
        <input
          ref={inputRef}
          id={`${uid}-input`}
          className="tag-input"
          value={query}
          placeholder={txn.tags.length ? 'Add…' : 'Add a tag, like “Vacation”'}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKey}
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-activedescendant={open && options[active] ? optId(active) : undefined}
          aria-autocomplete="list"
          aria-describedby={`${uid}-status`}
          autoComplete="off"
          spellCheck={false}
          maxLength={40}
        />
      </div>
      {open && (
        <ul ref={listRef} id={listId} className="tag-options" role="listbox" aria-label="Tags" aria-multiselectable="true">
          {options.length === 0 && <li className="tag-options-empty">No tags yet. Type a name to create one.</li>}
          {options.map((o, i) => (
            <li
              key={o.kind === 'tag' ? o.tag.id : '__create'}
              id={optId(i)}
              data-i={i}
              role="option"
              aria-selected={o.kind === 'tag' ? selected.has(o.tag.id) : false}
              className={`tag-opt${i === active ? ' is-active' : ''}${o.kind === 'create' ? ' is-create' : ''}`}
              onPointerMove={() => setActive(i)}
              // Keep focus in the input so the list stays open for more picks.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => void choose(o)}
            >
              {o.kind === 'tag' ? (
                <>
                  <span className="tag-opt-check" aria-hidden="true">
                    {selected.has(o.tag.id) && <Icon name="check" />}
                  </span>
                  <span className="tag-opt-dot" style={{ '--h': o.tag.hue } as CSSProperties} aria-hidden="true" />
                  <span className="truncate">{o.tag.name}</span>
                  <span className="tag-opt-count num">{o.tag.count}</span>
                </>
              ) : (
                <>
                  <Icon name="plus" />
                  <span className="truncate">
                    Create “{o.name}”
                  </span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <span className="field-hint" id={`${uid}-status`} aria-live="polite">
        {status || 'Tags group spending across categories, like a trip or a project.'}
      </span>
    </div>
  );
}
