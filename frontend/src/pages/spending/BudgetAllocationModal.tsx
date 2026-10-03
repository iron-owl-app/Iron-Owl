import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import type { BudgetMonth } from '../../api';
import { Modal } from '../../components/Modal';
import { formatMoney } from '../../lib/format';
import type { Section } from './GroupCard';
import { allocationPreview, parseAllocationAmount } from './workspaceMath';

export function BudgetAllocationModal({ open, bm, sections, initialCategory, onClose, onAdd, onAddCategory }: {
  open: boolean;
  bm: BudgetMonth;
  sections: Section[];
  initialCategory?: string | null;
  onClose: () => void;
  onAdd: (category: string, amount: number) => Promise<string | null>;
  onAddCategory?: () => void;
}) {
  const uid = useId();
  const [category, setCategory] = useState('');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const wasOpen = useRef(false);
  const latest = useRef(bm);
  latest.current = bm;
  const lines = bm.categories;
  const categoryIds = new Set(lines.map((line) => line.category));
  const ordered = sections.flatMap((section) => section.lines.filter((line) => categoryIds.has(line.category)).map((line) => ({ line, group: section.name })));
  const shown = new Set(ordered.map(({ line }) => line.category));
  for (const line of lines) if (!shown.has(line.category)) ordered.push({ line, group: 'Other categories' });
  const selected = lines.find((line) => line.category === category);
  const amount = parseAllocationAmount(draft);
  const available = bm.ready_to_assign;
  const preview = selected && amount !== null ? allocationPreview(selected, amount, available) : null;
  const past = !bm.is_current && !bm.is_future;
  const insufficient = amount !== null && preview !== null && preview.unplanned < 0;
  const insufficientText = `Only ${formatMoney(Math.max(0, available))} is not planned yet. Use a smaller amount or move money from another category.`;

  useEffect(() => {
    if (open && !wasOpen.current) {
      setCategory(initialCategory && lines.some((line) => line.category === initialCategory) ? initialCategory : ordered[0]?.line.category ?? '');
      setDraft('');
      setError(null);
    }
    wasOpen.current = open;
  }, [open, initialCategory, lines, ordered]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (saving.current) return;
    const current = latest.current;
    if (!current.is_current && !current.is_future) return setError('Past months are read-only.');
    const line = current.categories.find((item) => item.category === category);
    if (!line) return setError('Choose a category that is still in your budget.');
    const addition = parseAllocationAmount(draft);
    if (addition === null) return setError('Type an amount greater than zero, like 100.');
    if (allocationPreview(line, addition, current.ready_to_assign).unplanned < 0) {
      return setError(`Only ${formatMoney(Math.max(0, current.ready_to_assign))} is not planned yet. Use a smaller amount or move money from another category.`);
    }
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await onAdd(category, addition);
      if (result) setError(result);
      else onClose();
    } catch {
      setError('The plan could not be saved. Your amount is still here; try again.');
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }

  return (
    <Modal open={open} title="Give money a plan" subtitle="Choose a category and how much to add." onClose={onClose} busy={busy} className="bud-allocation">
      <form onSubmit={submit} noValidate>
        <p className="bud-allocation-available">Available to plan: <strong className="num">{formatMoney(available)}</strong></p>
        {past && <p className="bud-form-hint is-warn" role="status">Past months are read-only.</p>}
        {ordered.length ? (
          <>
            <label className="bud-field" htmlFor={`${uid}-category`}><span>Category</span>
              <select id={`${uid}-category`} value={category} disabled={busy || past} onChange={(e) => { setCategory(e.target.value); setError(null); }}>
                {!selected && <option value="">Choose a category</option>}
                {ordered.map(({ line, group }) => <option key={line.category} value={line.category}>{line.name} · {group}{line.goal ? ' · Goal' : line.debt ? ' · Debt plan' : line.category === bm.savings_category ? ' · Savings' : ''}</option>)}
              </select>
            </label>
            <label className="bud-field" htmlFor={`${uid}-amount`}><span>Amount to add</span>
              <span className="bud-money"><span aria-hidden="true">$</span><input id={`${uid}-amount`} className="num" inputMode="decimal" autoComplete="off" value={draft} disabled={busy || past}
                onChange={(e) => { setDraft(e.target.value); setError(null); }} aria-invalid={!!error || insufficient || undefined}
                aria-describedby={`${uid}-helper${error || insufficient ? ` ${uid}-error` : ''}`} /></span>
            </label>
            {selected && <dl className="bud-allocation-preview" aria-live="polite" aria-label="Plan preview">
              <div className="bud-allocation-preview-row"><dt>{selected.name} plan</dt><dd className="num">{formatMoney(selected.assigned)} → {preview ? formatMoney(preview.planned) : '—'}</dd></div>
              <div className="bud-allocation-preview-row"><dt>Left in {selected.name}</dt><dd className="num">{formatMoney(selected.available)} → {preview ? formatMoney(preview.left) : '—'}</dd></div>
              <div className="bud-allocation-preview-row"><dt>Not planned yet</dt><dd className="num">{formatMoney(available)} → {preview ? formatMoney(preview.unplanned) : '—'}</dd></div>
            </dl>}
          </>
        ) : <p>No categories to plan yet.{onAddCategory && !past && <button type="button" className="bud-btn" onClick={() => { onClose(); onAddCategory(); }}>Add category</button>}</p>}
        <p id={`${uid}-helper`} className="bud-form-hint">This changes your plan. It does not move money between accounts.</p>
        {(error || insufficient) && <p id={`${uid}-error`} className="bud-form-hint is-warn" role="alert">{error || insufficientText}</p>}
        <div className="bud-allocation-actions">
          <button type="button" className="bud-btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="bud-btn bud-btn-primary" disabled={busy || past || !selected || amount === null || insufficient}>
            {busy ? 'Saving…' : amount === null ? 'Add to plan' : `Add ${formatMoney(amount)} to plan`}
          </button>
        </div>
      </form>
    </Modal>
  );
}
