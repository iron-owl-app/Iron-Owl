import { useId, type CSSProperties } from 'react';
import type { MonthlyReport } from '../../../api';
import { DUST, planRow, planRowLabel, planTotalText, sum, W, type CatFig, type PlanRow, type View } from '../../../lib/reportsMath';
import { groupHue } from '../../../lib/reports';
import { Icon } from '../../../components/Icon';

const keyOf = (f: CatFig) => (f.cat.group_id === null ? 'none' : String(f.cat.group_id));

/**
 * "Plan vs. what you spent" (README 1e): groups, then a group's categories (breadcrumb).
 * Each row is a button: name, "$471 of $650", a bar at spent ÷ plan with a lighter pace
 * extension for the current month, and the status words (amber when over or likely over).
 * Plans are `months[].planned` (a range adds its months). Empty groups are left out.
 */
export function PlanVsSpent({
  report,
  figs,
  v,
  group,
  groupName,
  hueFor,
  selected,
  onGroup,
  onCategory,
}: {
  report: MonthlyReport;
  /** Visible categories (bills left out when hidden). */
  figs: CatFig[];
  v: View;
  group: string | null;
  groupName: (key: string) => string;
  hueFor: (c: CatFig['cat']) => number;
  selected: string | null;
  onGroup: (key: string | null) => void;
  onCategory: (id: string) => void;
}) {
  const headId = useId();
  const shown = figs.filter((f) => f.spent > DUST || f.plan > DUST);
  const hasGroups = report.groups.length > 0;
  let rows: PlanRow[];
  let inScope: CatFig[];
  if (hasGroups && group === null) {
    const keys = [...report.groups.map((g) => String(g.id)), 'none'];
    rows = keys
      .map((k) => {
        const fs = shown.filter((f) => keyOf(f) === k);
        if (!fs.length) return null;
        return planRow(k, groupName(k), groupHue(report, k === 'none' ? null : Number(k)), fs, true, v.isCur);
      })
      .filter((r): r is PlanRow => r !== null);
    inScope = shown;
  } else {
    inScope = hasGroups ? shown.filter((f) => keyOf(f) === group) : shown;
    rows = inScope.map((f) => planRow(f.cat.id, f.cat.name, hueFor(f.cat), [f], false, v.isCur));
  }
  const totSpent = sum(inScope.map((f) => f.spent));
  const totPlan = sum(inScope.map((f) => f.plan));
  const anyPlan = shown.some((f) => f.plan > DUST);
  const atTop = !hasGroups || group === null;

  return (
    <section className="rpo-card rpo-pva" aria-labelledby={headId}>
      <div className="rpo-head">
        <h2 id={headId}>Plan vs. what you spent</h2>
        <span className="rpo-hint">{hasGroups && group === null ? 'Click a group to see its categories' : 'Click a category for details'}</span>
      </div>
      {hasGroups && (
        <nav className="rpo-crumbs" aria-label="Breakdown level">
          {group === null ? (
            <span className="rpo-crumb is-here" aria-current="location">
              All groups
            </span>
          ) : (
            <>
              <button type="button" className="rpo-crumb" onClick={() => onGroup(null)}>
                All groups
              </button>
              <Icon name="chevronRight" />
              <span className="rpo-crumb is-here" aria-current="location">
                {groupName(group)}
              </span>
            </>
          )}
        </nav>
      )}
      {!anyPlan && shown.length > 0 && (
        <p className="rpo-sub">There’s no budget plan for {v.period === 'month' ? 'this month' : 'these months'}, so this shows what you spent.</p>
      )}
      {rows.length === 0 ? (
        <p className="rpo-none">Nothing planned or spent here in this period.</p>
      ) : (
        <ul className="rpo-rows">
          {rows.map((r) => (
            <li key={r.key}>
              <button
                type="button"
                className={`rpo-row${r.status.warn ? ' is-warn' : ''}`}
                style={{ '--h': r.hue } as CSSProperties}
                aria-pressed={r.group ? undefined : selected === r.key}
                aria-label={planRowLabel(r)}
                onClick={() => (r.group ? onGroup(r.key) : onCategory(r.key))}
              >
                <span className="rpo-row-top">
                  <span className="rpo-row-name">
                    <span className="rpo-dot" aria-hidden="true" />
                    {r.name}
                    {r.bill && <span className="rpo-bill">Bill</span>}
                  </span>
                  <span className="rpo-row-amt num">
                    <strong>{W(r.spent)}</strong>
                    {r.plan > DUST && <span> of {W(r.plan)}</span>}
                  </span>
                  {r.group && <Icon name="chevronRight" />}
                </span>
                {r.plan > DUST && (
                  <span className="rpo-pbar" aria-hidden="true">
                    {r.ghost > r.fill && <span className="rpo-pbar-ghost" style={{ width: `${r.ghost.toFixed(1)}%` }} />}
                    <span className="rpo-pbar-fill" style={{ width: `${r.fill.toFixed(1)}%` }} />
                  </span>
                )}
                <span className="rpo-row-status">{r.status.text}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="rpo-foot">
        <span>{atTop ? 'Total' : `${groupName(group!)} total`}</span>
        <span className="num">{planTotalText(totSpent, totPlan)}</span>
      </div>
    </section>
  );
}

