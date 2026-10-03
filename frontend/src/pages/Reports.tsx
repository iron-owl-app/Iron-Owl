import { useCallback, useRef, useState, type KeyboardEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { readPref, writePref } from '../lib/prefs';
import { useUpdated } from '../state';
import { OverviewTab } from './reports/OverviewTab';
import { HabitsTab } from './reports/Habits';
import { YoyTab } from './reports/YoyTab';
import { SpendingTab } from './reports/SpendingTab';
import { DebtTab } from './reports/DebtTab';
import './reports/reports.css';

/**
 * Reports (Release 3.14, "Reports v2"): one page with tabs, Overview · Habits · Year over year ·
 * Spending · Paying off debt. The tab is in the URL (`/reports?tab=overview|habits|yoy|spending|debt`)
 * and remembered in the `fintrack.ui.reports.tab` pref. Older values still open something sensible:
 * `monthly` (the old overview) opens Overview, and `year` (Year in review, dropped in 3.10) opens
 * Year over year. Adding a report later: one more entry in TABS and its panel.
 */

type TabId = 'overview' | 'habits' | 'yoy' | 'spending' | 'debt';

const TABS: { id: TabId; label: string; icon: string; desc: string }[] = [
  {
    id: 'overview',
    label: 'Overview',
    icon: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
    desc: 'A quick look at your money: what you spent, how it compares, and what to do next.',
  },
  {
    id: 'habits',
    label: 'Habits',
    icon: 'M4 6.5h16v13.5H4ZM4 10.5h16M8.5 3.5v5M15.5 3.5v5M8 14.5h2M14 14.5h2M8 17.5h2',
    desc: 'Patterns in when and how you spend, with ideas to try.',
  },
  {
    id: 'yoy',
    label: 'Year over year',
    icon: 'M5 20V12M9 20V8M15 20v-6M19 20V6M3 20h18',
    desc: 'This year compared with last year, by category and by store.',
  },
  {
    id: 'spending',
    label: 'Spending',
    icon: 'M6 3.5h12v17l-3-2-3 2-3-2-3 2ZM9 8.5h6M9 12.5h6',
    desc: 'Where your money went, month by month. To plan what you’ll spend, use Budget.',
  },
  { id: 'debt', label: 'Paying off debt', icon: 'M4 17 9 12l4 4 7-8M15 8h5v5', desc: 'When you’ll be debt-free, and ways to get there sooner.' },
];

const TAB_PREF = 'reports.tab';
const isTab = (v: unknown): v is TabId => TABS.some((t) => t.id === v);
/** Older ids, kept so a stored pref or an old link still opens a tab. */
const LEGACY: Record<string, TabId> = { monthly: 'overview', year: 'yoy' };
/** A tab id from the URL or the pref, or null when it isn't one. */
function toTab(v: unknown): TabId | null {
  if (isTab(v)) return v;
  return typeof v === 'string' && Object.prototype.hasOwnProperty.call(LEGACY, v) ? LEGACY[v]! : null;
}

/** The header's "Updated 5 minutes ago" line: the same words as the sidebar and other pages. */
function useUpdatedLine(): string | null {
  return useUpdated().text;
}

export function ReportsPage() {
  const [params, setParams] = useSearchParams();
  const [saved] = useState<TabId>(() => toTab(readPref<string>(TAB_PREF, 'overview', (v): v is string => toTab(v) !== null)) ?? 'overview');
  const tab: TabId = toTab(params.get('tab')) ?? saved;
  const active = TABS.find((t) => t.id === tab)!;
  const updated = useUpdatedLine();

  const goTab = useCallback(
    (t: TabId) => {
      writePref(TAB_PREF, t);
      // A tab's own params (?month=, ?cat=) don't carry over to another tab.
      setParams(new URLSearchParams({ tab: t }), { replace: true });
    },
    [setParams],
  );

  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  function onTabKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    const last = TABS.length - 1;
    let j = -1;
    if (e.key === 'ArrowRight') j = i === last ? 0 : i + 1;
    else if (e.key === 'ArrowLeft') j = i === 0 ? last : i - 1;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = last;
    if (j < 0) return;
    e.preventDefault();
    goTab(TABS[j]!.id);
    tabRefs.current[j]?.focus();
  }

  return (
    <div className="rp-shell">
      <header className="rp-head">
        <h1>Reports</h1>
        <p>{active.desc}</p>
        {updated && <p className="rp-updated">{updated}</p>}
      </header>
      <div className="rp-tabbar">
        <div role="tablist" aria-label="Reports" className="rp-tabs">
          {TABS.map((t, i) => {
            const on = t.id === tab;
            return (
              <button
                key={t.id}
                ref={(el) => {
                  tabRefs.current[i] = el;
                }}
                type="button"
                role="tab"
                id={`rp-tab-${t.id}`}
                aria-selected={on}
                aria-controls={`rp-panel-${t.id}`}
                tabIndex={on ? 0 : -1}
                className="rp-tab"
                onClick={() => goTab(t.id)}
                onKeyDown={(e) => onTabKey(e, i)}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d={t.icon} />
                </svg>
                {t.label}
              </button>
            );
          })}
        </div>
      </div>
      <div role="tabpanel" id={`rp-panel-${tab}`} aria-labelledby={`rp-tab-${tab}`} className="rp-panel">
        {tab === 'overview' ? (
          <OverviewTab />
        ) : tab === 'habits' ? (
          <HabitsTab />
        ) : tab === 'yoy' ? (
          <YoyTab />
        ) : tab === 'spending' ? (
          <SpendingTab />
        ) : (
          <DebtTab />
        )}
      </div>
    </div>
  );
}
