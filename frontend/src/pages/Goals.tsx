import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, errorMessage, type Goal, type GoalKind, type GoalPatch, type GoalsState } from '../api';
import { useApi } from '../lib/useApi';
import { moneyShort, monthName } from '../lib/budget';
import { planChange } from '../lib/goals';
import { Skeleton } from '../components/ui';
import { ErrorPanel } from '../components/ErrorPanel';
import { UndoToast } from '../components/UndoToast';
import { useToast } from '../components/Toast';
import { GoalCard } from './goals/GoalCard';
import { GoalWindow, type GoalWindowMode, type GoalWindowResult } from './goals/GoalWindow';
import './goals/goals.css';

/*
 * Goals (design D8, layout B): money set aside each month for something that matters. Each goal
 * is a Budget category, so its monthly amount comes out of the month's money like any other
 * plan. Every change shows the shared Undo message (12 s). `?edit=<id>` (from Budget's
 * "Edit goal →") opens that goal's window; `?new=emergency|save` opens a new one.
 */

const m = moneyShort;

interface Msg {
  key: number;
  title?: string;
  body: string;
  undo?: () => Promise<GoalsState>;
}

export function GoalsPage() {
  const toast = useToast();
  const goals = useApi(() => api.goals.get(), []);
  const accounts = useApi(() => api.accounts.list(), []);
  const [win, setWin] = useState<GoalWindowMode | null>(null);
  const [msg, setMsg] = useState<Msg | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const msgKey = useRef(0);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const [params, setParams] = useSearchParams();

  const st = goals.data;
  const apply = (s: GoalsState) => goals.setData(s);
  function say(body: string, undo?: () => Promise<GoalsState>, title?: string) {
    msgKey.current += 1;
    setMsg({ key: msgKey.current, title, body, undo });
  }

  // Deep links: ?edit=<id> and ?new=emergency|save (the parameter is then removed).
  useEffect(() => {
    if (!st) return;
    const edit = params.get('edit');
    const fresh = params.get('new');
    if (!edit && !fresh) return;
    const g = edit ? st.goals.find((x) => String(x.id) === edit) : undefined;
    if (g && st.budget_ready) setWin({ mode: 'edit', goal: g });
    else if (fresh && st.budget_ready) setWin({ mode: 'new', kind: fresh === 'emergency' && !hasEmergency(st) ? 'emergency' : 'save' });
    const next = new URLSearchParams(params);
    next.delete('edit');
    next.delete('new');
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st, params]);

  if (goals.error && !st) return <ErrorPanel error={goals.error} onRetry={goals.reload} />;

  async function run<T>(id: number | null, work: () => Promise<T>, failTitle: string): Promise<T | null> {
    setBusyId(id);
    try {
      return await work();
    } catch (err) {
      toast.push({ tone: 'error', title: failTitle, body: errorMessage(err) });
      return null;
    } finally {
      setBusyId(null);
    }
  }

  async function changeMonthly(g: Goal, v: number): Promise<boolean> {
    // Undo puts this month's plan back exactly as it was (a lower plan may have stopped at its floor).
    const back: GoalPatch = { monthly: g.monthly };
    if (g.in_budget && st) back.plan_before = { month: st.month, planned: g.planned_this_month };
    const res = await run(g.id, () => api.goals.update(g.id, { monthly: v }), `Couldn’t change ${g.name}`);
    if (!res) return false;
    apply(res);
    say(`${g.name} is now ${m(v)} a month. Budget is updated.`, () => api.goals.update(g.id, back));
    return true;
  }

  function setAside(g: Goal) {
    if (!st || !g.needed_monthly) return;
    const extra = planChange(g, g.needed_monthly, g.target);
    if (extra > Math.max(0, st.not_planned) + 0.004) {
      say(`Only ${m(Math.max(0, st.not_planned))} isn’t planned yet. Free up money on the Budget page first.`);
      return;
    }
    void changeMonthly(g, g.needed_monthly);
  }

  async function remove(g: Goal, reason: 'deleted' | 'spent'): Promise<boolean> {
    const res = await run(g.id, () => api.goals.remove(g.id, reason), reason === 'spent' ? `Couldn’t finish ${g.name}` : `Couldn’t delete ${g.name}`);
    if (!res) return false;
    apply(res.state);
    const u = res.undo;
    say(`${reason === 'spent' ? `${g.name} is finished. Nice work.` : 'Goal deleted.'} ${whereMoneyWent(res.kept_in, u.released)}`.trim(), () => api.goals.restore(u));
    return true;
  }

  function onWindowDone(r: GoalWindowResult) {
    const was = win;
    setWin(null);
    apply(r.state);
    if (was?.mode === 'new') {
      const id = r.goalId;
      // Undo of an add takes it off, then says where its money is now: a new category's money
      // goes to Not planned yet (Already saved too, even the part that came from the emergency
      // fund); Emergency savings that the fund took over is put back exactly as it was before.
      const undo =
        id !== undefined
          ? async () => {
              const x = await api.goals.undoAdd(id);
              if (x.kept_in) {
                const back = x.released > 0.004 ? `, and the ${m(x.released)} it took is back in Not planned yet` : '';
                say(`${r.name} was taken off. ${x.kept_in} is back as it was before${back}.`);
              } else {
                // Never more than what actually went back (money spent since the add stays spent).
                const fromFund = Math.min(r.fromFund, x.released);
                const fund = fromFund > 0.004 ? `, including ${m(fromFund)} that came from ${r.fundName}` : '';
                say(`${r.name} was taken off. ${whereMoneyWent(null, x.released, fund)}`.trim());
              }
              return x.state;
            }
          : undefined;
      say(`${r.name} was added. It’s now a category in Budget.`, undo);
    } else if (was?.mode === 'edit') {
      const g = was.goal;
      const before: GoalPatch | null | undefined = r.before;
      const undo = before && Object.keys(before).length ? () => api.goals.update(g.id, before) : undefined;
      say(g.in_budget ? `${r.name} was saved.` : `${r.name} is now a category in Budget.`, undo);
    }
    titleRef.current?.focus({ preventScroll: true });
  }

  async function onUndo() {
    const cur = msg;
    if (!cur?.undo || undoBusy) return;
    setUndoBusy(true);
    try {
      apply(await cur.undo());
      setMsg((x) => (x?.key === cur.key ? null : x));
      titleRef.current?.focus({ preventScroll: true });
    } catch (err) {
      msgKey.current += 1;
      setMsg({ key: msgKey.current, title: 'Couldn’t undo that.', body: errorMessage(err) });
    } finally {
      setUndoBusy(false);
    }
  }

  const list = st?.goals ?? [];
  const ready = st?.budget_ready ?? true;
  const inBudget = list.filter((g) => g.in_budget);
  const openNew = (kind: GoalKind) => setWin({ mode: 'new', kind: kind === 'emergency' && st && hasEmergency(st) ? 'save' : kind });

  return (
    <div className="gl-page">
      <header className="gl-head">
        <div className="gl-head-text">
          <h1 ref={titleRef} tabIndex={-1}>
            Goals
          </h1>
          <p>Money you set aside each month for something that matters. It comes out of your month’s amount on the Budget page, like any other category.</p>
        </div>
        {st && ready && (
          <button type="button" className="gl-btn gl-btn-lg gl-btn-primary" onClick={() => openNew('save')}>
            + New goal
          </button>
        )}
      </header>

      {!st ? (
        <div className="gl-grid" aria-busy="true" aria-label="Loading goals">
          {[0, 1, 2].map((i) => (
            <div className="gl-card" key={i}>
              <Skeleton width="40%" height={14} />
              <Skeleton width="60%" height={22} />
              <Skeleton width="100%" height={12} />
              <Skeleton width="100%" height={48} />
            </div>
          ))}
        </div>
      ) : (
        <>
          {!ready && (
            <section className="gl-empty" aria-labelledby="gl-setup-h">
              <h2 id="gl-setup-h">Set up your Budget first</h2>
              <p>Goals are part of your Budget: each one is a category there, and its monthly amount comes out of the month’s money. It takes a couple of minutes.</p>
              <Link className="gl-btn gl-btn-lg gl-btn-primary" to="/spending">
                Set up your Budget first <span aria-hidden="true">→</span>
              </Link>
            </section>
          )}

          {ready && inBudget.length > 0 && (
            <div className="gl-summary">
              <span className="gl-summary-text">
                <strong className="num">{m(st.monthly_total)} a month</strong>{' '}
                <span>
                  goes to your goals. That’s part of {monthName(st.month)}’s {m(st.month_money)}.
                </span>
              </span>
              <Link className="gl-link" to="/spending">
                See it in Budget <span aria-hidden="true">→</span>
              </Link>
            </div>
          )}

          {ready && list.length === 0 && (
            <section className="gl-empty" aria-labelledby="gl-empty-h">
              <h2 id="gl-empty-h">Start with one goal</h2>
              <p>Pick something you want money ready for. Iron Owl will tell you how much to set aside each month, and keep track as it grows.</p>
              <div className="gl-starts">
                <button type="button" className="gl-start" onClick={() => openNew('emergency')}>
                  <span className="gl-start-t">Emergency fund</span>
                  <span className="gl-start-d">Money for surprises, like a car repair or a doctor’s bill.</span>
                </button>
                <button type="button" className="gl-start" onClick={() => openNew('save')}>
                  <span className="gl-start-t">Save up for something</span>
                  <span className="gl-start-d">Like holiday gifts, a trip or new tires, by a date you pick.</span>
                </button>
              </div>
            </section>
          )}

          {list.length > 0 && (
            <div className="gl-grid">
              {list.map((g) => (
                <GoalCard
                  key={g.id}
                  g={g}
                  notPlanned={Math.max(0, st.not_planned)}
                  budgetReady={ready}
                  busy={busyId === g.id}
                  onChangeMonthly={(v) => changeMonthly(g, v)}
                  onSetAside={() => setAside(g)}
                  onEdit={() => setWin({ mode: 'edit', goal: g })}
                  onSpent={() => void remove(g, 'spent')}
                />
              ))}
            </div>
          )}

          {ready && list.length > 0 && (
            <p className="gl-foot">Each goal is a category in Budget under “Other and saving”. At the end of the month, Iron Owl counts what you set aside as saved.</p>
          )}

          <GoalWindow
            open={win !== null}
            win={win}
            state={st}
            accounts={accounts.data ?? []}
            onClose={() => setWin(null)}
            onDone={onWindowDone}
            onDelete={async (g) => {
              const done = await remove(g, 'deleted');
              if (done) {
                setWin(null);
                titleRef.current?.focus({ preventScroll: true });
              }
              return done;
            }}
          />
        </>
      )}

      <UndoToast
        message={msg && { key: msg.key, title: msg.title, body: msg.body, undo: !!msg.undo }}
        onUndo={() => void onUndo()}
        undoBusy={undoBusy}
        onClose={() => setMsg(null)}
      />
    </div>
  );
}

const hasEmergency = (s: GoalsState) => s.goals.some((g) => g.kind === 'emergency');

/** Where a deleted goal's money is now, in the words the Budget page uses. */
function whereMoneyWent(keptIn: string | null, r: number, extra = ''): string {
  if (keptIn) return `${keptIn} is back in your Budget, with its money.`;
  if (r > 0.004) return `The ${m(r)} still in it is back in Not planned yet on the Budget page${extra}.`;
  if (r < -0.004) return `It had ${m(-r)} more spent than set aside, so that comes out of Not planned yet.`;
  return '';
}
