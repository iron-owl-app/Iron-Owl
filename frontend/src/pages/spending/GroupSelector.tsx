import { useId, type CSSProperties } from 'react';
import { Icon } from '../../components/Icon';
import { formatMoney, plural } from '../../lib/format';
import type { Section } from './GroupCard';
import { groupSelectorStats, groupWorkspaceTotals } from './workspaceMath';

export interface GroupSelectorProps {
  sections: Section[];
  savingsId: string | null;
  selectedKey: string;
  onSelect: (key: string) => void;
  onAdd: () => void;
  disabled?: boolean;
}

type Stats = ReturnType<typeof groupSelectorStats>;

/** Line 2: "$500.00 left", "$450.00 reserved" or "$20.00 over". */
function leftText(st: Stats): string {
  if (st.kind === 'empty') return 'No categories yet';
  if (st.kind === 'noplan') return 'No plan yet';
  return st.over ? `${formatMoney(-st.left)} over` : `${formatMoney(st.left)} ${st.kind === 'reserved' ? 'reserved' : 'left'}`;
}

/** Line 3: "$600.00 used · 60%"; reserved-only groups say what was paid. */
function usedText(st: Extract<Stats, { kind: 'spending' | 'reserved' }>): string {
  if (st.kind === 'reserved') return st.spent > 0.004 ? `${formatMoney(st.spent)} paid` : 'Nothing used yet';
  return st.percent === null ? `${formatMoney(st.spent)} used` : `${formatMoney(st.spent)} used · ${st.percent}%`;
}

/** The same "left of planned" words for the narrow list and its summary line. */
function leftOfText(st: Stats): string {
  return st.kind === 'empty' || st.kind === 'noplan' ? leftText(st) : `${leftText(st)} of ${formatMoney(st.planned)}`;
}

/** Real groups remain in their existing order; decorative letters do not require new metadata. */
export function GroupSelector({ sections, savingsId, selectedKey, onSelect, onAdd, disabled = false }: GroupSelectorProps) {
  const uid = useId();
  const selected = sections.find((section) => section.key === selectedKey);
  const selectedOverCount = selected ? groupWorkspaceTotals(selected.lines, savingsId).overCount : 0;
  return (
    <section className="bud-group-navigation" aria-labelledby={`${uid}-title`}>
      <header className="bud-group-navigation-head">
        <h2 id={`${uid}-title`}>Groups</h2>
        {!disabled && <button type="button" className="bud-btn" onClick={onAdd}><Icon name="plus" />Add group</button>}
      </header>
      <div className="bud-group-selector-grid" role="group" aria-label="Choose a budget group">
        {sections.map((section) => {
          const t = groupWorkspaceTotals(section.lines, savingsId);
          const st = groupSelectorStats(section.lines, savingsId);
          const warning = t.overCount ? `${plural(t.overCount, 'category', 'categories')} over plan` : '';
          return <button key={section.key} type="button" className={`bud-group-selector${selectedKey === section.key ? ' is-selected' : ''}`} style={{ '--h': section.hue } as CSSProperties} aria-pressed={selectedKey === section.key} onClick={() => onSelect(section.key)}>
            <span className="bud-selector-letter" aria-hidden="true">{section.name.slice(0, 1).toUpperCase()}</span>
            <span className="bud-selector-copy"><span className="bud-selector-name">{section.name}</span>{st.kind === 'empty' || st.kind === 'noplan'
                ? <span className="bud-selector-summary">{leftText(st)}</span>
                : <>
                  <span className="bud-selector-money num"><span className={`bud-selector-left${st.over ? ' is-warn' : ''}`}>{leftText(st)}</span><span className="bud-selector-of">of {formatMoney(st.planned)}</span></span>
                  <span className="bud-selector-bar" aria-hidden="true"><span className={`bud-selector-fill${st.overPlan ? ' is-warn' : ''}`} style={{ width: `${st.fill * 100}%` }} /></span>
                  <span className={`bud-selector-used num${st.overPlan ? ' is-warn' : ''}`}>{usedText(st)}</span>
                </>}
              {warning && <span className="bud-selector-warning">⚠ {warning}</span>}</span>
            <Icon name="chevronDown" className="bud-selector-chevron" />
          </button>;
        })}
      </div>
      <div className="bud-group-selector-narrow">
        <label className="bud-field" htmlFor={`${uid}-select`}><span>Choose a group</span></label>
        <select id={`${uid}-select`} className="bud-input" value={selectedKey} onChange={(e) => onSelect(e.target.value)}>
          {sections.map((section) => {
            const count = groupWorkspaceTotals(section.lines, savingsId).overCount;
            return <option key={section.key} value={section.key}>{section.name} · {leftOfText(groupSelectorStats(section.lines, savingsId))}{count ? ` · ${plural(count, 'category', 'categories')} over plan` : ''}</option>;
          })}
        </select>
        {selected && <span className="bud-selector-summary num">{leftOfText(groupSelectorStats(selected.lines, savingsId))}</span>}
        {selectedOverCount > 0 && <span className="bud-selector-warning" aria-live="polite">⚠ {plural(selectedOverCount, 'category', 'categories')} over plan in this group</span>}
      </div>
      {!sections.length && <p className="bud-group-empty">No groups yet.{disabled ? '' : ' Add a group to organize your categories.'}</p>}
    </section>
  );
}
