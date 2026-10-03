import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import type { Goal } from '../../api';
import { parseMoneyInput } from '../../components/ui';
import { moneyShort, round2, str } from '../../lib/budget';
import { goalMonthLong, goalMonthShort, planChange } from '../../lib/goals';

/*
 * One goal (design D8 layout B "Goal card"): type, name and Edit; saved of target; the timeline
 * bar (saved in front of what's projected by the due date); a sentence in words; the "Each
 * month" box with Change; and the amber "short" box or the green "It's ready" box.
 */

const m = moneyShort;

export function GoalCard({
  g,
  notPlanned,
  budgetReady,
  busy,
  onChangeMonthly,
  onSetAside,
  onEdit,
  onSpent,
}: {
  g: Goal;
  /** Budget's Not planned yet this month. */
  notPlanned: number;
  budgetReady: boolean;
  busy: boolean;
  /** Resolves true once saved (the field closes). */
  onChangeMonthly: (v: number) => Promise<boolean>;
  onSetAside: () => void;
  onEdit: () => void;
  onSpent: () => void;
}) {
  const headId = useId();
  const reached = g.status === 'reached';
  const behind = g.status === 'behind';
  const [editing, setEditing] = useState(false);
  const typeLabel = g.kind === 'emergency' ? 'Emergency fund' : 'Saving for something';

  const border = !g.in_budget ? '' : behind && !editing ? ' is-behind' : reached ? ' is-reached' : '';
  return (
    <section className={`gl-card${border}`} aria-labelledby={headId}>
      <div className="gl-card-top">
        <span className="gl-card-id">
          <span className="gl-type">{typeLabel}</span>
          <h2 id={headId} className="gl-name">
            {g.name}
          </h2>
        </span>
        {budgetReady && (
          <button type="button" className="gl-btn gl-btn-sm" onClick={onEdit} aria-label={`Edit ${g.name}`}>
            Edit
          </button>
        )}
      </div>

      {!g.in_budget ? (
        <NotInBudget g={g} budgetReady={budgetReady} onEdit={onEdit} />
      ) : (
        <>
          <Timeline g={g} />
          {!reached && (
            <>
              <p className="gl-line">{statusLine(g)}</p>
              <MonthlyBox g={g} notPlanned={notPlanned} editing={editing} setEditing={setEditing} busy={busy} onSave={onChangeMonthly} />
            </>
          )}
          {behind && !editing && (
            <div className="gl-box is-warn">
              <p>
                {g.needed_monthly !== null && g.needed_monthly > 0
                  ? `That’s ${m(g.short_by ?? 0)} short. Set aside ${m(g.needed_monthly)} a month to be ready, or give it more time.`
                  : `That’s ${m(g.short_by ?? 0)} short. Give it more time to be ready.`}
              </p>
              <div className="gl-box-btns">
                {g.needed_monthly !== null && g.needed_monthly > 0 && (
                  <button type="button" className="gl-btn gl-btn-strong" onClick={onSetAside} disabled={busy}>
                    Set aside {m(g.needed_monthly)}
                  </button>
                )}
                <button type="button" className="gl-btn gl-btn-warn" onClick={onEdit}>
                  Give it more time
                </button>
              </div>
            </div>
          )}
          {reached && (
            <div className="gl-box is-ok">
              <p>
                <strong>It’s ready.</strong> You have {m(g.saved)} for {g.name}
                {g.account_label ? ` in ${g.account_label}` : ''}. Iron Owl stopped setting money aside for it.
              </p>
              <div className="gl-box-btns">
                <button type="button" className="gl-btn gl-btn-strong" onClick={onSpent} disabled={busy}>
                  I spent it
                </button>
                <button type="button" className="gl-btn gl-btn-ok" onClick={onEdit}>
                  Raise the goal
                </button>
              </div>
            </div>
          )}
        </>
      )}
      {g.account_label && <span className="gl-where">Kept in {g.account_label}</span>}
    </section>
  );
}

/** "At $90 a month, you'll have $600 by December 2026. You're on track." and friends. */
function statusLine(g: Goal): string {
  if (g.kind === 'emergency') {
    return g.monthly > 0.004 && g.projected_month
      ? `At ${m(g.monthly)} a month, you’ll reach ${m(g.target)} around ${goalMonthLong(g.projected_month)}.`
      : 'Set a monthly amount to see when you’ll get there.';
  }
  if (!g.due_month) return g.monthly > 0.004 ? `${m(g.monthly)} a month goes to it.` : 'Set a monthly amount to see when you’ll get there.';
  if (g.months_to_save !== null && g.months_to_save < 1) return `It was due in ${goalMonthLong(g.due_month)}.`;
  if (g.monthly < 0.005) {
    return g.needed_monthly
      ? `Nothing is set aside for it each month yet. Set aside ${m(g.needed_monthly)} a month to be ready by ${goalMonthLong(g.due_month)}.`
      : `Nothing is set aside for it each month yet.`;
  }
  return `At ${m(g.monthly)} a month, you’ll have ${m(g.projected)} by ${goalMonthLong(g.due_month)}.${g.status === 'on_track' ? ' You’re on track.' : ''}`;
}

function Timeline({ g }: { g: Goal }) {
  const reached = g.status === 'reached';
  const pct = g.target > 0 ? Math.min(100, Math.max(0, (g.saved / g.target) * 100)) : 0;
  let projPct = pct;
  if (!reached && g.kind === 'save' && g.due_month) projPct = g.target > 0 ? Math.min(100, (g.projected / g.target) * 100) : 0;
  if (!reached && g.kind === 'emergency' && g.projected_month) projPct = 100;
  const hasDate = !reached && g.kind === 'save' && !!g.due_month;
  const end = reached
    ? 'Done'
    : g.kind === 'save'
      ? g.due_month
        ? `Ready by ${goalMonthShort(g.due_month)}`
        : ''
      : g.projected_month
        ? `About ${goalMonthShort(g.projected_month)}`
        : 'No date';
  return (
    <div className="gl-numbers">
      <div className="gl-amounts">
        <span className="gl-saved num">{m(g.saved)}</span>
        <span className="gl-of num">of {m(g.target)}</span>
      </div>
      <div
        className="gl-track-wrap"
        role="progressbar"
        aria-label={`${g.name}: saved so far`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
        aria-valuetext={`${m(g.saved)} of ${m(g.target)} saved`}
      >
        <div className="gl-track">
          <span className={`gl-proj${g.status === 'behind' ? ' is-behind' : ''}`} style={{ width: `${projPct.toFixed(1)}%` }} />
          <span className={`gl-fill${reached ? ' is-done' : ''}`} style={{ width: `${pct.toFixed(1)}%` }} />
        </div>
        {hasDate && <span className="gl-marker" aria-hidden="true" />}
      </div>
      <div className="gl-track-labels" aria-hidden={reached ? undefined : true}>
        <span>Now</span>
        <span>{end}</span>
      </div>
    </div>
  );
}

function NotInBudget({ g, budgetReady, onEdit }: { g: Goal; budgetReady: boolean; onEdit: () => void }) {
  return (
    <>
      <p className="gl-line">
        Goal: <strong className="num">{m(g.target)}</strong>
        {g.due_month ? ` by ${goalMonthLong(g.due_month)}` : ''}
        {g.monthly > 0.004 ? `, ${m(g.monthly)} a month` : ''}.
      </p>
      <div className="gl-box is-plain">
        <p>
          <strong>Not in your Budget yet.</strong>{' '}
          {budgetReady
            ? 'Open it and press Save to make it a category in Budget. Then Iron Owl sets money aside for it each month.'
            : 'It joins your Budget once your Budget is set up.'}
        </p>
        {budgetReady && (
          <div className="gl-box-btns">
            <button type="button" className="gl-btn gl-btn-accent" onClick={onEdit}>
              Add it to my Budget
            </button>
          </div>
        )}
      </div>
    </>
  );
}

/** "Each month $90 [Change]", which turns into a typed amount with Save and Cancel. */
function MonthlyBox({
  g,
  notPlanned,
  editing,
  setEditing,
  busy,
  onSave,
}: {
  g: Goal;
  notPlanned: number;
  editing: boolean;
  setEditing: (v: boolean) => void;
  busy: boolean;
  onSave: (v: number) => Promise<boolean>;
}) {
  const uid = useId();
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const changeRef = useRef<HTMLButtonElement>(null);
  const wasEditing = useRef(false);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    } else if (wasEditing.current) {
      changeRef.current?.focus();
    }
    wasEditing.current = editing;
  }, [editing]);

  const parsed = parseMoneyInput(draft);
  const bad = parsed === null || Number.isNaN(parsed) || parsed < 0;
  const v = bad ? 0 : round2(parsed);
  // What this month's plan changes by: the new plan − the old one (the server keeps money moved
  // in or out on the Budget page as it is).
  const extra = planChange(g, v, g.target);
  const over = !bad && extra > notPlanned + 0.004;
  let hint: { text: string; warn: boolean } | null = null;
  if (editing) {
    if (bad && draft.trim() !== '') hint = { text: 'Type an amount, like 90.', warn: true };
    else if (over) hint = { text: `Only ${m(notPlanned)} isn’t planned yet this month. Use a smaller amount, or take money from another category on the Budget page.`, warn: true };
    else if (!bad && extra > 0.004) hint = { text: `This takes ${m(extra)} more a month from Not planned yet in Budget.`, warn: false };
    else if (!bad && extra < -0.004) hint = { text: `This month, ${m(-extra)} goes back to Not planned yet in Budget.`, warn: false };
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (bad || over || saving) return;
    if (Math.abs(v - g.monthly) < 0.005) return setEditing(false);
    setSaving(true);
    const ok = await onSave(v);
    setSaving(false);
    if (ok) setEditing(false);
  }

  return (
    <>
      <div className="gl-month">
        {!editing ? (
          <>
            <span className="gl-month-text">
              Each month <strong className="num">{m(g.monthly)}</strong>
            </span>
            <button
              ref={changeRef}
              type="button"
              className="gl-btn gl-btn-sm gl-btn-accent"
              onClick={() => {
                setDraft(str(g.monthly));
                setEditing(true);
              }}
              disabled={busy}
              aria-label={`Change how much goes to ${g.name} each month, now ${m(g.monthly)}`}
            >
              Change
            </button>
          </>
        ) : (
          <form
            className="gl-month-form"
            onSubmit={submit}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                setEditing(false);
              }
            }}
            noValidate
          >
            <span className="gl-money is-strong">
              <span aria-hidden="true">$</span>
              <input
                ref={inputRef}
                className="num"
                inputMode="decimal"
                autoComplete="off"
                aria-label={`Each month for ${g.name}`}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                aria-invalid={bad && draft.trim() !== '' ? true : over || undefined}
                aria-describedby={hint ? `${uid}-hint` : undefined}
              />
            </span>
            <button type="submit" className="gl-btn gl-btn-primary" disabled={bad || over || saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="gl-btn" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
          </form>
        )}
      </div>
      {hint && (
        <p id={`${uid}-hint`} className={`gl-hint${hint.warn ? ' is-warn' : ''}`} aria-live="polite">
          {hint.text}
        </p>
      )}
    </>
  );
}
