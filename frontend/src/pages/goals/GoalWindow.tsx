import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api, errorMessage, type Account, type Goal, type GoalKind, type GoalPatch, type GoalsState } from '../../api';
import { Modal } from '../../components/Modal';
import { parseMoneyInput } from '../../components/ui';
import { moneyShort, round2, str } from '../../lib/budget';
import { dueChoices, goalMonthLong, monthsToSave, neededMonthly, planChange, planFor, projectedBy, reachMonth } from '../../lib/goals';
import { addMonths } from '../../lib/budget';

/*
 * The New goal / Edit goal window (design D8 "New / Edit goal window", 560px). The Budget note
 * box compares the plan with Budget's Not planned yet and blocks anything above it, like the
 * server does (the plan goes first; "Already saved" then takes what's left of Not planned yet,
 * then the rest comes out of the emergency fund).
 */

const m = moneyShort;

export type GoalWindowMode = { mode: 'new'; kind: GoalKind } | { mode: 'edit'; goal: Goal };

export interface GoalWindowResult {
  state: GoalsState;
  /** New goals: the new id. */
  goalId?: number;
  name: string;
  alreadySaved: number;
  /** New goals: the part of Already saved that came out of the emergency fund, and its name. */
  fromFund: number;
  fundName: string;
  /** Edit: the fields as they were (for Undo); null when there's nothing to put back. */
  before?: GoalPatch | null;
}

const isSavingsLike = (a: Account) => a.plaid_subtype === 'savings' || /sav/i.test(a.name);
export const accountLabel = (a: Account) => `${a.name}${a.mask ? ` ··${a.mask}` : ''}`;

function amount(raw: string): { v: number; bad: boolean } {
  const p = parseMoneyInput(raw);
  if (p === null) return { v: 0, bad: false };
  if (Number.isNaN(p) || p < 0) return { v: 0, bad: true };
  return { v: round2(p), bad: false };
}

export function GoalWindow({
  open,
  win,
  state,
  accounts,
  onClose,
  onDone,
  onDelete,
}: {
  open: boolean;
  win: GoalWindowMode | null;
  state: GoalsState;
  accounts: Account[];
  onClose: () => void;
  onDone: (r: GoalWindowResult) => void;
  /** Delete goal (edit only): the page deletes it and shows Undo. */
  onDelete: (g: Goal) => Promise<boolean>;
}) {
  const uid = useId();
  const T = state.month;
  const editGoal = win?.mode === 'edit' ? win.goal : null;
  const isNew = win?.mode === 'new';
  const [kind, setKind] = useState<GoalKind>('save');
  const [name, setName] = useState('');
  const [targetT, setTargetT] = useState('');
  const [due, setDue] = useState(addMonths(T, 3));
  const [savedT, setSavedT] = useState('');
  const [monthlyT, setMonthlyT] = useState('');
  const [accountId, setAccountId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  const bankAccounts = useMemo(() => accounts.filter((a) => !a.hidden && a.category === 'bank'), [accounts]);
  const emergencyGoal = state.goals.find((g) => g.kind === 'emergency' && g.in_budget) ?? null;

  useEffect(() => {
    if (!open || !win) return;
    setError(null);
    setBusy(false);
    if (win.mode === 'new') {
      const k = win.kind;
      const pick = [...bankAccounts].sort((a, b) => Number(isSavingsLike(b)) - Number(isSavingsLike(a)))[0];
      setKind(k);
      setName(k === 'emergency' ? 'Emergency fund' : '');
      setTargetT(k === 'emergency' && state.emergency_suggestion ? str(state.emergency_suggestion) : '');
      setDue(addMonths(T, 3));
      setSavedT('');
      setMonthlyT('');
      setAccountId(pick ? String(pick.id) : '');
    } else {
      const g = win.goal;
      setKind(g.kind);
      setName(g.name);
      setTargetT(str(g.target));
      setDue(g.due_month ?? addMonths(T, 3));
      setSavedT('');
      setMonthlyT(str(g.monthly));
      setAccountId(g.account_id !== null ? String(g.account_id) : '');
    }
    requestAnimationFrame(() => nameRef.current?.focus());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, win]);

  if (!win) return null;

  const isEf = kind === 'emergency';
  // Linking an old goal may carry "Already saved" too (the backend accepts it only then). A goal
  // whose category was removed on the Budget page (category_id still set) comes back without it.
  const canSeed = isNew || (editGoal !== null && !editGoal.in_budget && editGoal.category_id === null);
  const target = amount(targetT);
  const monthly = amount(monthlyT);
  const already = canSeed ? amount(savedT) : { v: 0, bad: false };
  // What the goal starts with: an existing goal's envelope, or (a new emergency fund) what's
  // already in Emergency savings, which it takes over.
  const base = editGoal ? (editGoal.in_budget ? editGoal.saved : 0) : isEf ? (state.emergency_available ?? 0) : 0;
  const saved = round2(base + already.v);
  const months = !isEf ? monthsToSave(T, due) : 0;
  const nd = !isEf && target.v > 0 ? neededMonthly(target.v, saved, months) : 0;

  // What this month's plan takes from Not planned yet. Editing a goal in the Budget changes its
  // plan by the difference between the new and the old plan (the server's rule: money moved in
  // or out on the Budget page stays), so a name-only edit is 0 and never blocks Save. A new
  // goal, or one joining the Budget, takes its whole plan.
  const extra = editGoal?.in_budget ? planChange(editGoal, monthly.v, target.v) : planFor(monthly.v, target.v, saved);
  const notPlanned = Math.max(0, state.not_planned);
  const overPlan = extra > notPlanned + 0.004;

  // "Already saved": Not planned yet first (after the plan), then the emergency fund.
  const left = Math.max(0, round2(notPlanned - Math.max(0, extra)));
  const fromUnplanned = Math.min(already.v, left);
  const fromEf = round2(already.v - fromUnplanned);
  const efName = emergencyGoal?.name ?? 'Emergency savings';
  const efMoney = isEf ? 0 : emergencyGoal ? round2(emergencyGoal.saved + emergencyGoal.planned_this_month) : (state.emergency_available ?? 0);
  const overSeed = fromEf > efMoney + 0.004;

  let result = '';
  if (target.v > 0) {
    if (saved >= target.v - 0.004) result = 'That’s already enough.';
    else if (isEf) {
      const r = reachMonth(T, target.v, saved, monthly.v);
      result = r ? `You’ll reach ${m(target.v)} around ${goalMonthLong(r)}.` : '';
    } else if (monthly.v > 0 || saved > 0) {
      const proj = projectedBy(target.v, saved, monthly.v, months);
      const short = round2(target.v - saved - monthly.v * months);
      result = short <= 0.004 ? `By ${goalMonthLong(due)} you’ll have ${m(proj)}. That’s enough.` : `By ${goalMonthLong(due)} you’ll have ${m(proj)}, ${m(short)} short.`;
    }
  }

  const budText = overPlan
    ? `Only ${m(notPlanned)} isn’t planned yet this month. Use a smaller amount, or take money from another category on the Budget page.`
    : extra > 0.004
      ? `This takes ${m(extra)}${editGoal?.in_budget ? ' more' : ''} a month from your Budget. This month ${m(notPlanned)} isn’t planned yet, so it fits.`
      : extra < -0.004
        ? `This month, ${m(-extra)} goes back to Not planned yet in your Budget.`
        : `Your goals are part of your Budget. This month ${m(notPlanned)} isn’t planned yet.`;

  let seedHint: { text: string; warn: boolean } | null = null;
  if (already.bad) seedHint = { text: 'Type an amount, like 200.', warn: true };
  else if (already.v > 0.004) {
    if (overSeed) {
      seedHint = {
        text:
          efMoney > 0.004
            ? `That’s more than you have: ${m(left)} isn’t planned yet and ${efName} has ${m(efMoney)}.`
            : `Only ${m(left)} isn’t planned yet this month, so you can count up to that much.`,
        warn: true,
      };
    } else if (fromEf > 0.004) {
      seedHint = {
        text: fromUnplanned > 0.004 ? `${m(fromUnplanned)} comes out of Not planned yet and ${m(fromEf)} out of ${efName}.` : `It comes out of ${efName}, since nothing else is free this month.`,
        warn: false,
      };
    } else seedHint = { text: 'It comes out of Not planned yet in Budget.', warn: false };
  }

  const badAmount = target.bad || monthly.bad || already.bad;
  const ok = !!name.trim() && target.v > 0 && !badAmount && !overPlan && !overSeed;
  const why = ok ? undefined : overPlan || overSeed ? 'That’s more than isn’t planned yet' : 'Add a name and an amount first';

  const choicesNow = dueChoices(T);
  const choices = [...choicesNow];
  if (!isEf && due && !choices.includes(due)) choices.unshift(due);

  const suggestion = isNew && isEf ? state.emergency_suggestion : null;
  const basisLine =
    suggestion && state.emergency_monthly
      ? state.emergency_basis === 'bills'
        ? `Many people aim for 3 months of bills. Your bills come to about ${m(Math.round(state.emergency_monthly))} a month, so about ${m(suggestion)}.`
        : `Many people aim for 3 months of spending. Yours comes to about ${m(Math.round(state.emergency_monthly))} a month, so about ${m(suggestion)}.`
      : null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ok || busy || !win) return;
    setBusy(true);
    setError(null);
    const acct = accountId ? Number(accountId) : null;
    try {
      if (win.mode === 'new') {
        const res = await api.goals.create({
          kind,
          name: name.trim(),
          target: target.v,
          due_month: isEf ? null : due,
          monthly: monthly.v,
          already_saved: already.v,
          account_id: acct,
        });
        const { goal_id, ...st } = res;
        onDone({ state: st, goalId: goal_id, name: name.trim(), alreadySaved: already.v, fromFund: fromEf, fundName: efName });
      } else {
        const g = win.goal;
        const patch: GoalPatch = {};
        const before: GoalPatch = {};
        if (name.trim() !== g.name) {
          patch.name = name.trim();
          before.name = g.name;
        }
        if (Math.abs(target.v - g.target) >= 0.005) {
          patch.target = target.v;
          before.target = g.target;
        }
        if (!isEf && due !== g.due_month) {
          patch.due_month = due;
          // Undo can only send back a month that's still allowed (next month … 24 ahead); an
          // old due month that has passed stays changed.
          if (g.due_month && choicesNow.includes(g.due_month)) before.due_month = g.due_month;
        }
        if (Math.abs(monthly.v - g.monthly) >= 0.005) {
          patch.monthly = monthly.v;
          before.monthly = g.monthly;
        }
        if (acct !== g.account_id) {
          patch.account_id = acct;
          before.account_id = g.account_id;
        }
        // Undo puts this month's plan back exactly (a lower plan may have stopped at its floor,
        // so raising it again by the rule wouldn't give the same Not planned yet).
        if (g.in_budget && (patch.target !== undefined || patch.monthly !== undefined)) {
          before.plan_before = { month: T, planned: g.planned_this_month };
        }
        if (!g.in_budget) {
          // Saving an old goal once links it (a name alone is enough to send).
          patch.name = name.trim();
          if (already.v > 0) patch.already_saved = already.v;
        } else if (!Object.keys(patch).length) {
          setBusy(false);
          onClose();
          return;
        }
        // A due month that's no longer in the list (it passed) stays as it was.
        const res = await api.goals.update(g.id, patch);
        onDone({ state: res, name: name.trim(), alreadySaved: already.v, fromFund: fromEf, fundName: efName, before: g.in_budget ? before : null });
      }
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  const title = editGoal ? `Edit ${editGoal.name}` : 'New goal';
  const moneyInput = (id: string, label: string, value: string, set: (v: string) => void, invalid: boolean, describedBy?: string) => (
    <span className="gl-money is-lg">
      <span aria-hidden="true">$</span>
      <input
        id={id}
        className="num"
        inputMode="decimal"
        autoComplete="off"
        aria-label={label}
        value={value}
        onChange={(e) => {
          set(e.target.value);
          setError(null);
        }}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
      />
    </span>
  );

  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      busy={busy}
      width={560}
      className="gl-modal"
      footer={
        <>
          {editGoal && (
            <button
              type="button"
              className="gl-btn gl-btn-danger"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                const done = await onDelete(editGoal);
                if (!done) setBusy(false);
              }}
            >
              Delete goal
            </button>
          )}
          <span className="gl-foot-gap" />
          <button type="button" className="gl-btn gl-btn-lg" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form={`${uid}-form`} className="gl-btn gl-btn-lg gl-btn-primary" disabled={!ok || busy} title={why}>
            {busy ? 'Saving…' : editGoal ? 'Save' : 'Add goal'}
          </button>
        </>
      }
    >
      <form id={`${uid}-form`} className="gl-form" onSubmit={submit} noValidate>
        {error && (
          <div className="gl-box is-warn" role="alert">
            <p>{error}</p>
          </div>
        )}
        {isNew && (
          <div role="radiogroup" aria-label="Kind of goal" className="gl-kinds">
            <KindTile
              on={isEf}
              label="Emergency fund"
              sub={emergencyGoal ? 'You already have one' : 'Money for surprises'}
              disabled={!!emergencyGoal}
              onPick={() => {
                setKind('emergency');
                if (!name.trim()) setName('Emergency fund');
                if (!targetT && state.emergency_suggestion) setTargetT(str(state.emergency_suggestion));
              }}
            />
            <KindTile
              on={!isEf}
              label="Save up for something"
              sub="Ready by a date you pick"
              onPick={() => {
                setKind('save');
                if (name === 'Emergency fund') setName('');
              }}
            />
          </div>
        )}

        <Field id={`${uid}-name`} label="What it’s for">
          <input
            ref={nameRef}
            id={`${uid}-name`}
            className="gl-input"
            value={name}
            maxLength={60}
            autoComplete="off"
            placeholder="For example, Holiday gifts"
            onChange={(e) => {
              setName(e.target.value);
              setError(null);
            }}
          />
        </Field>

        <Field id={`${uid}-target`} label="How much you want saved">
          <div className="gl-inline">
            {moneyInput(`${uid}-target`, 'How much you want saved', targetT, setTargetT, target.bad, basisLine ? `${uid}-basis` : undefined)}
            {suggestion !== null && suggestion > 0 && Math.abs(target.v - suggestion) >= 0.005 && (
              <button type="button" className="gl-btn gl-btn-dashed" onClick={() => setTargetT(str(suggestion))}>
                Use {m(suggestion)}
              </button>
            )}
          </div>
          {basisLine && (
            <span id={`${uid}-basis`} className="gl-help">
              {basisLine}
            </span>
          )}
        </Field>

        {!isEf && (
          <Field id={`${uid}-due`} label="When do you need it?">
            <div className="gl-inline">
              <select id={`${uid}-due`} className="gl-input gl-select" value={due} onChange={(e) => setDue(e.target.value)} aria-describedby={`${uid}-due-n`}>
                {choices.map((c) => (
                  <option key={c} value={c}>
                    {goalMonthLong(c)}
                  </option>
                ))}
              </select>
              <span id={`${uid}-due-n`} className="gl-help">
                {months === 1 ? '1 month to save' : `${months} months to save`}
              </span>
            </div>
          </Field>
        )}

        {canSeed ? (
          <Field id={`${uid}-saved`} label="Already saved (optional)">
            {moneyInput(`${uid}-saved`, 'Already saved', savedT, setSavedT, already.bad || overSeed, seedHint ? `${uid}-saved-h` : undefined)}
            {isNew && isEf && (state.emergency_available ?? 0) > 0.004 && (
              <span className="gl-help">You already have {m(state.emergency_available ?? 0)} in Emergency savings. It becomes part of this fund.</span>
            )}
            {seedHint && (
              <span id={`${uid}-saved-h`} className={`gl-help${seedHint.warn ? ' is-warn' : ''}`} aria-live="polite">
                {seedHint.text}
              </span>
            )}
          </Field>
        ) : (
          editGoal && (
            <div className="gl-field">
              <span className="gl-label">Saved so far</span>
              <span className="gl-readonly num">{m(editGoal.saved)}</span>
            </div>
          )
        )}

        <Field id={`${uid}-monthly`} label="Set aside each month">
          <div className="gl-inline">
            {moneyInput(`${uid}-monthly`, 'Set aside each month', monthlyT, setMonthlyT, monthly.bad || overPlan, `${uid}-result`)}
            {!isEf && target.v > 0 && nd > 0 && Math.abs(nd - monthly.v) >= 0.005 && (
              <button type="button" className="gl-btn gl-btn-dashed" onClick={() => setMonthlyT(str(nd))}>
                Use {m(nd)} a month
              </button>
            )}
          </div>
          <span id={`${uid}-result`} className="gl-help gl-result" aria-live="polite">
            {result}
          </span>
        </Field>

        <div className={`gl-budnote${overPlan ? ' is-warn' : ''}`} aria-live="polite">
          {budText}
        </div>

        {bankAccounts.length > 0 && (
          <Field id={`${uid}-acct`} label="Keep the money in">
            <select id={`${uid}-acct`} className="gl-input gl-select" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
              {bankAccounts.map((a) => (
                <option key={a.id} value={String(a.id)}>
                  {accountLabel(a)}
                </option>
              ))}
              <option value="">I’ll decide later</option>
            </select>
          </Field>
        )}
      </form>
    </Modal>
  );
}

function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="gl-field">
      <label className="gl-label" htmlFor={id}>
        {label}
      </label>
      {children}
    </div>
  );
}

function KindTile({ on, label, sub, disabled, onPick }: { on: boolean; label: string; sub: string; disabled?: boolean; onPick: () => void }) {
  return (
    <button type="button" role="radio" aria-checked={on} className={`gl-kind${on ? ' is-on' : ''}`} onClick={onPick} disabled={disabled}>
      <span className="gl-kind-t">{label}</span>
      <span className="gl-kind-d">{sub}</span>
    </button>
  );
}
