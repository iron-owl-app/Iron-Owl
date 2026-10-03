import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { readPref, writePref } from '../lib/prefs';
import { BudgetDetailed } from './spending/BudgetDetailed';
import { BudgetSimple } from './spending/BudgetSimple';
import './spending/budget.css';

/*
 * Budget (route /spending; the nav calls it "Budget"). Two views of the same envelope budget
 * (SPEC "Release 3.6: Budget for a beginner"):
 * - Simple (default): group workspace, with month navigation and read-only history.
 * - Detailed: the Release 3.1 "Assign every dollar" screen, unchanged.
 * The choice is a UI preference. `?cat=<category id>` opens the page at that category in
 * either view (the parameter is cleared once read).
 */

export type BudgetView = 'simple' | 'detailed';
const VIEW_PREF = 'budget.view';
const isView = (v: unknown): v is BudgetView => v === 'simple' || v === 'detailed';

export function SpendingPage() {
  const [view, setViewState] = useState<BudgetView>(() => readPref<BudgetView>(VIEW_PREF, 'simple', isView));
  const [params, setParams] = useSearchParams();
  const [focusCat, setFocusCat] = useState<string | null>(() => params.get('cat'));

  // Read `?cat=` once, then take it off the address so a reload doesn't jump again.
  useEffect(() => {
    const cat = params.get('cat');
    if (cat === null) return;
    setFocusCat(cat);
    const next = new URLSearchParams(params);
    next.delete('cat');
    setParams(next, { replace: true });
  }, [params, setParams]);

  function setView(v: BudgetView) {
    setViewState(v);
    writePref(VIEW_PREF, v);
    window.scrollTo({ top: 0 });
  }

  return view === 'detailed' ? (
    <BudgetDetailed focusCat={focusCat} onSimple={() => setView('simple')} />
  ) : (
    <BudgetSimple focusCat={focusCat} onFocused={() => setFocusCat(null)} onDetailed={() => setView('detailed')} />
  );
}
