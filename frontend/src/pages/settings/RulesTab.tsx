import { useEffect, useId, useRef, useState, type CSSProperties, type DragEvent, type KeyboardEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, type Rule, type RuleInput, type RulePreview, type TxnCategory } from '../../api';
import { useApi } from '../../lib/useApi';
import { plural } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { GripIcon } from './CategoriesTab';
import { DialogActions, StDialog, dollars, monthDay, useFail, useSay } from './parts';

type RuleDraft = {
  mode: 'new' | 'edit';
  id?: number;
  field: Rule['field'];
  op: Rule['op'];
  text: string;
  useAmount: boolean;
  amountOp: 'gt' | 'lt';
  amountText: string;
  action: Rule['action'];
  category: string;
};

const hueStyle = (h: number) => ({ '--h': h }) as CSSProperties;
/** Rules made here match the store name anywhere (`any`); older rules keep what they had. */
const subject = (field: Rule['field']) => (field === 'name' ? 'Bank description' : 'Store');
const parseAmount = (t: string) => {
  const n = Number(t.replace(/[$,\s]/g, ''));
  return Number.isFinite(n) && n > 0 ? n : 0;
};

/**
 * Settings › Rules (design D7 tab 2, replaces the Rules & alerts page): the rules in the order
 * they're checked (first match wins). Drag a row, or use ArrowUp / ArrowDown on its grip, to
 * reorder (Undo). The rule window shows a live preview of the past purchases it fits and which
 * would change. Rules always apply to past purchases that weren't set by hand.
 */
export function RulesTab() {
  const say = useSay();
  const fail = useFail();
  const [params, setParams] = useSearchParams();
  const rules = useApi(() => api.rules.list(), []);
  const cats = useApi(() => api.categories.list(), []);
  const [rd, setRd] = useState<RuleDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const drag = useRef<number | null>(null);
  const [dragI, setDragI] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const uid = useId();

  const list = [...(rules.data ?? [])].sort((a, b) => a.position - b.position);
  const catById = new Map((cats.data ?? []).map((c) => [c.id, c]));
  const visibleCats = (cats.data ?? []).filter((c) => !c.hidden);

  const openNew = () =>
    setRd({
      mode: 'new',
      field: 'any',
      op: 'is',
      text: '',
      useAmount: false,
      amountOp: 'gt',
      amountText: '',
      action: 'category',
      category: visibleCats.find((c) => c.kind === 'spending')?.id ?? visibleCats[0]?.id ?? '',
    });

  // Search › "New rule" and the old /rules?new=1 link.
  const wantNew = params.get('new') === '1';
  useEffect(() => {
    if (!wantNew || !cats.data) return;
    setParams(
      (p) => {
        const next = new URLSearchParams(p);
        next.delete('new');
        return next;
      },
      { replace: true },
    );
    openNew();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantNew, cats.data]);

  const openEdit = (r: Rule) =>
    setRd({
      mode: 'edit',
      id: r.id,
      field: r.field,
      op: r.op,
      text: r.text,
      useAmount: r.amount_op !== null && r.amount !== null,
      amountOp: r.amount_op ?? 'gt',
      amountText: r.amount !== null ? String(r.amount) : '',
      action: r.action,
      category: r.category ?? '',
    });

  // ---------------------------------------------------------------- order and on/off

  async function reorder(from: number, to: number) {
    if (to > from) to--;
    if (to === from || to < 0 || to >= list.length) return;
    const next = [...list];
    const [r] = next.splice(from, 1);
    next.splice(to, 0, r!);
    const prevIds = list.map((x) => x.id);
    rules.setData(next.map((x, i) => ({ ...x, position: i })));
    try {
      rules.setData(await api.rules.reorder(next.map((x) => x.id)));
      say(`Rule for ${r!.text} moved.`, { undo: async () => rules.setData(await api.rules.reorder(prevIds)) });
    } catch (e) {
      rules.reload();
      fail('Couldn’t move the rule', e);
    }
  }

  async function toggle(r: Rule) {
    rules.setData((prev) => (prev ?? []).map((x) => (x.id === r.id ? { ...x, enabled: !r.enabled } : x)));
    try {
      const u = await api.rules.update(r.id, { enabled: !r.enabled });
      rules.setData((prev) => (prev ?? []).map((x) => (x.id === u.id ? u : x)));
    } catch (e) {
      rules.setData((prev) => (prev ?? []).map((x) => (x.id === r.id ? r : x)));
      fail('Couldn’t change the rule', e);
    }
  }

  function endDrag() {
    drag.current = null;
    setDragI(null);
    setOver(null);
  }

  function rowDrag(i: number) {
    return {
      draggable: true,
      onDragStart: (e: DragEvent) => {
        drag.current = i;
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', list[i]?.text ?? '');
        setDragI(i);
      },
      onDragOver: (e: DragEvent<HTMLDivElement>) => {
        if (drag.current === null) return;
        e.preventDefault();
        const b = e.currentTarget.getBoundingClientRect();
        const o = e.clientY < b.top + b.height / 2 ? i : i + 1;
        setOver((cur) => (cur === o ? cur : o));
      },
      onDrop: (e: DragEvent) => {
        e.preventDefault();
        const d = drag.current;
        const o = over;
        endDrag();
        if (d !== null && o !== null) void reorder(d, o);
      },
      onDragEnd: endDrag,
    };
  }

  function onGripKey(e: KeyboardEvent<HTMLButtonElement>, i: number, r: Rule) {
    let to = -1;
    if (e.key === 'ArrowUp' && i > 0) to = i - 1;
    else if (e.key === 'ArrowDown' && i < list.length - 1) to = i + 2;
    if (to < 0) return;
    e.preventDefault();
    void reorder(i, to).then(() => requestAnimationFrame(() => document.getElementById(`${uid}-grip-${r.id}`)?.focus()));
  }

  // ---------------------------------------------------------------- the window

  const text = rd?.text.trim() ?? '';
  const amount = rd?.useAmount ? parseAmount(rd.amountText) : 0;
  const draftOk = !!rd && !!text && (rd.action !== 'category' || !!rd.category) && (!rd.useAmount || amount > 0);
  const draftInput: RuleInput | null = rd
    ? {
        field: rd.field,
        op: rd.op,
        text,
        amount_op: rd.useAmount && amount > 0 ? rd.amountOp : null,
        amount: rd.useAmount && amount > 0 ? amount : null,
        action: rd.action,
        category: rd.action === 'category' ? rd.category : null,
        enabled: true,
      }
    : null;
  const preview = usePreview(draftInput && text && (draftInput.action !== 'category' || draftInput.category) ? draftInput : null, rd?.id);

  function outcome(r: Pick<Rule, 'text' | 'action' | 'category'>, changed: boolean): string {
    const to = r.action === 'transfer' ? 'will count as transfers, not spending' : `will go to ${catById.get(r.category ?? '')?.name ?? 'that category'}`;
    return `Purchases from ${r.text} ${to}.${changed ? ' Past ones were changed too.' : ''}`;
  }

  async function save() {
    if (!rd || !draftInput || !draftOk) return;
    setBusy(true);
    try {
      if (rd.mode === 'new') {
        const r = await api.rules.create({ ...draftInput, first: true });
        const changed = (preview.data?.would_change ?? r.matches) > 0;
        say(`Rule added at the top. ${outcome(r, changed)}`);
      } else if (rd.id !== undefined) {
        const cur = list.find((x) => x.id === rd.id);
        const patch: Partial<RuleInput> = {};
        if (!cur || cur.op !== draftInput.op) patch.op = draftInput.op;
        if (!cur || cur.text !== draftInput.text) patch.text = draftInput.text;
        if (!cur || cur.amount_op !== draftInput.amount_op || cur.amount !== draftInput.amount) {
          patch.amount_op = draftInput.amount_op;
          patch.amount = draftInput.amount;
        }
        if (draftInput.action === 'category' && (!cur || cur.category !== draftInput.category)) patch.category = draftInput.category;
        const r = Object.keys(patch).length ? await api.rules.update(rd.id, patch) : cur!;
        say(`Rule saved. ${outcome(r, (preview.data?.would_change ?? 0) > 0)}`);
      }
      setRd(null);
      rules.reload();
    } catch (e) {
      fail('Couldn’t save the rule', e);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    const r = rd?.id !== undefined ? list.find((x) => x.id === rd.id) : undefined;
    if (!r) return;
    setBusy(true);
    try {
      await api.rules.remove(r.id);
      rules.setData((prev) => (prev ?? []).filter((x) => x.id !== r.id));
      setRd(null);
      const back: RuleInput & { position: number } = {
        field: r.field,
        op: r.op,
        text: r.text,
        amount_op: r.amount_op,
        amount: r.amount,
        action: r.action,
        category: r.category,
        enabled: r.enabled,
        position: r.position,
      };
      say('Rule deleted. Purchases it sorted go back to automatic.', {
        undo: async () => {
          await api.rules.create(back);
          rules.reload();
        },
      });
      rules.reload();
    } catch (e) {
      fail('Couldn’t delete the rule', e);
    } finally {
      setBusy(false);
    }
  }

  // ---------------------------------------------------------------- render

  const loading = (rules.loading && !rules.data) || (cats.loading && !cats.data);
  const error = (rules.error && !rules.data) || (cats.error && !cats.data);
  const catOptions = (current: string): TxnCategory[] => {
    const cur = catById.get(current);
    return cur && cur.hidden ? [cur, ...visibleCats] : visibleCats;
  };

  return (
    <section className="st-card is-clip" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Rules</h2>
          <p className="st-desc">
            Rules put purchases into the right category for you. After every update, Iron Owl checks them from top to bottom and uses the first one that fits.
          </p>
          <p className="st-desc-2">Drag a row to change the order.</p>
        </div>
        <button type="button" className="st-btn st-btn-primary" onClick={openNew} disabled={loading}>
          + New rule
        </button>
      </div>

      {loading ? (
        <div className="st-loading" aria-busy="true" aria-label="Loading rules">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} height={52} style={{ borderRadius: 10 }} />
          ))}
        </div>
      ) : error ? (
        <p className="st-error">
          Iron Owl couldn’t load your rules.{' '}
          <button
            type="button"
            className="st-link st-link-inline"
            onClick={() => {
              rules.reload();
              cats.reload();
            }}
          >
            Try again
          </button>
        </p>
      ) : list.length === 0 ? (
        <div className="st-empty">No rules yet. Click New rule to make one.</div>
      ) : (
        <div className="st-table-scroll">
          <div role="table" aria-label="Rules" className="st-table">
            <div role="row" className="st-tr st-th">
              <span />
              <span role="columnheader">When</span>
              <span role="columnheader">Then</span>
              <span role="columnheader">Used</span>
              <span role="columnheader">On</span>
              <span />
            </div>
            {list.map((r, i) => {
              const c = r.category ? catById.get(r.category) : undefined;
              const slot = drag.current !== null && over === i && i !== drag.current && i !== drag.current + 1;
              const endSlot = drag.current !== null && over === list.length && i === list.length - 1 && drag.current !== list.length - 1;
              return (
                <div
                  key={r.id}
                  role="row"
                  className={`st-tr${dragI === i ? ' is-dragging' : ''}${slot ? ' is-slot' : ''}${endSlot ? ' is-slot-end' : ''}${r.enabled ? '' : ' is-off'}`}
                  {...rowDrag(i)}
                >
                  <span role="cell">
                    <button
                      type="button"
                      id={`${uid}-grip-${r.id}`}
                      className="st-grip"
                      aria-label={`Move the rule for ${r.text}. Drag, or use the up and down arrow keys.`}
                      title="Drag to move"
                      onKeyDown={(e) => onGripKey(e, i, r)}
                    >
                      <GripIcon />
                    </button>
                  </span>
                  <span role="cell" className="st-when">
                    {subject(r.field)} {r.op === 'is' ? 'is' : 'contains'} <strong className="st-store">{r.text}</strong>
                    {r.amount_op && r.amount !== null ? ` and ${r.amount_op === 'gt' ? 'over' : 'under'} ${dollars(r.amount)}` : ''}
                  </span>
                  <span role="cell" className="st-then">
                    {r.action === 'transfer' ? (
                      <span className="st-cat-pill">Transfer, not spending</span>
                    ) : (
                      <span className="st-cat-pill">
                        <span className="st-cdot" style={hueStyle(c?.hue ?? 262)} aria-hidden="true" />
                        {c?.name ?? 'Uncategorized'}
                      </span>
                    )}
                  </span>
                  <span role="cell">{r.matches > 0 ? <span className="st-used">{plural(r.matches, 'purchase')}</span> : <span className="st-chip is-plain is-nodot">Not used yet</span>}</span>
                  <span role="cell">
                    <button type="button" role="switch" className="st-switch-bare" aria-checked={r.enabled} aria-label={`Rule for ${r.text} on`} onClick={() => void toggle(r)}>
                      <span className="st-switch-track" aria-hidden="true">
                        <span className="st-switch-knob" />
                      </span>
                    </button>
                  </span>
                  <span role="cell" className="st-td-end">
                    <button type="button" className="st-btn st-btn-plain st-btn-sm" onClick={() => openEdit(r)} aria-label={`Edit the rule for ${r.text}`}>
                      Edit
                    </button>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
      <p className="st-foot-row">When you change a purchase’s category on the Transactions page, Iron Owl offers to make a rule for that store.</p>

      <StDialog open={rd !== null} size={660} title={rd?.mode === 'edit' ? 'Edit rule' : 'New rule'} onClose={() => setRd(null)} onSubmit={() => void save()} busy={busy}>
        {rd && (
          <>
            <div className="st-field">
              <span className="st-field-label" id={`${uid}-when`}>
                When the {rd.field === 'name' ? 'bank description' : 'store name'}
              </span>
              <div className="st-when-row">
                <div role="radiogroup" aria-labelledby={`${uid}-when`} className="st-options">
                  {(['is', 'contains'] as const).map((op) => (
                    <button
                      key={op}
                      type="button"
                      role="radio"
                      className="st-option st-option-tight"
                      aria-checked={rd.op === op}
                      tabIndex={rd.op === op ? 0 : -1}
                      onClick={() => setRd({ ...rd, op })}
                      onKeyDown={(e) => {
                        if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
                          e.preventDefault();
                          const next = op === 'is' ? 'contains' : 'is';
                          setRd({ ...rd, op: next });
                          (e.currentTarget.parentElement?.querySelector(`[data-op="${next}"]`) as HTMLElement | null)?.focus();
                        }
                      }}
                      data-op={op}
                    >
                      {op === 'is' ? 'is exactly' : 'contains'}
                    </button>
                  ))}
                </div>
                <input
                  className="st-input st-when-input"
                  value={rd.text}
                  maxLength={200}
                  autoComplete="off"
                  aria-label={rd.field === 'name' ? 'Bank description' : 'Store name'}
                  placeholder="For example, Paws Pet Supply"
                  onChange={(e) => setRd({ ...rd, text: e.target.value })}
                />
              </div>
            </div>
            <div className="st-amount-row">
              <label className="st-check">
                <input type="checkbox" checked={rd.useAmount} onChange={(e) => setRd({ ...rd, useAmount: e.target.checked })} />
                <span>Only when the amount is {rd.amountOp === 'lt' ? 'under' : 'over'}</span>
              </label>
              <span className={`st-money st-money-rule${rd.useAmount ? '' : ' is-off'}`}>
                <span aria-hidden="true">$</span>
                <input
                  className="st-input"
                  inputMode="decimal"
                  aria-label="Amount"
                  value={rd.amountText}
                  disabled={!rd.useAmount}
                  onChange={(e) => setRd({ ...rd, amountText: e.target.value.replace(/[^0-9.,]/g, '') })}
                  aria-invalid={rd.useAmount && amount <= 0 && rd.amountText !== '' ? true : undefined}
                />
              </span>
            </div>
            {rd.action === 'category' ? (
              <div className="st-field">
                <label className="st-field-label" htmlFor={`${uid}-cat`}>
                  Put it in
                </label>
                <select id={`${uid}-cat`} className="st-input" value={rd.category} onChange={(e) => setRd({ ...rd, category: e.target.value })}>
                  {catOptions(rd.category).map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : (
              <p className="st-help">Then Iron Owl counts it as a transfer between your accounts, not as spending.</p>
            )}
            <PreviewBox text={text} preview={preview} />
            <DialogActions
              left={
                rd.mode === 'edit' ? (
                  <button type="button" className="st-btn st-btn-danger st-btn-lg" onClick={() => void remove()} disabled={busy}>
                    Delete
                  </button>
                ) : undefined
              }
              onCancel={() => setRd(null)}
              submitLabel={rd.mode === 'edit' ? 'Save' : 'Add rule'}
              submitDisabled={!draftOk}
              busy={busy}
            />
          </>
        )}
      </StDialog>
    </section>
  );
}

interface PreviewState {
  data: RulePreview | null;
  loading: boolean;
  error: boolean;
}

/** POST /api/rules/preview for the rule being edited, 300 ms after the last change. */
function usePreview(input: RuleInput | null, ruleId: number | undefined): PreviewState {
  const [state, setState] = useState<PreviewState>({ data: null, loading: false, error: false });
  const key = input ? JSON.stringify({ ...input, ruleId }) : '';
  useEffect(() => {
    if (!input) {
      setState({ data: null, loading: false, error: false });
      return;
    }
    let live = true;
    setState((s) => ({ ...s, loading: true }));
    const h = window.setTimeout(() => {
      api.rules
        .preview(ruleId !== undefined ? { ...input, rule_id: ruleId } : input)
        .then((data) => live && setState({ data, loading: false, error: false }))
        .catch(() => live && setState({ data: null, loading: false, error: true }));
    }, 300);
    return () => {
      live = false;
      window.clearTimeout(h);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

function PreviewBox({ text, preview }: { text: string; preview: PreviewState }) {
  const d = preview.data;
  let summary: string;
  if (!text) summary = 'Type a store name to see which purchases fit.';
  else if (!d) summary = preview.error ? 'Iron Owl couldn’t check past purchases just now.' : 'Checking past purchases…';
  else if (d.matches === 0) summary = 'No past purchases fit this rule.';
  else summary = `Fits ${plural(d.matches, 'past purchase')} · ${d.would_change > 0 ? `${d.would_change} would change` : 'none would change'}`;
  return (
    <div className="st-preview" aria-live="polite" aria-busy={preview.loading || undefined}>
      <div className="st-preview-head">{summary}</div>
      {d && text && d.matches === 0 && <div className="st-preview-none">That’s fine. The rule will still sort new purchases from this store.</div>}
      {d &&
        text &&
        d.sample.map((t) => (
          <div className="st-preview-row" key={t.id}>
            <span className="st-preview-date">{monthDay(t.date)}</span>
            <span className="st-preview-main">
              <span className="st-preview-store">{t.merchant ?? t.name}</span>
              <span className="st-preview-cats">
                <s>{t.from?.name ?? 'Uncategorized'}</s>
                <span>
                  → <strong>{t.to.name}</strong>
                </span>
              </span>
            </span>
            <span className="st-preview-amt">{dollars(Math.abs(t.amount))}</span>
          </div>
        ))}
      {d && text && d.would_change > d.sample.length && <div className="st-preview-none">And {plural(d.would_change - d.sample.length, 'more')}.</div>}
    </div>
  );
}
