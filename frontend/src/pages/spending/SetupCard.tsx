import { useEffect, useId, useState } from 'react';
import { api, errorMessage, type BudgetSetupInfo, type BudgetSetupInput, type BudgetSetupKey } from '../../api';
import { Icon } from '../../components/Icon';
import { SkeletonRows } from '../../components/ui';
import { formatMoney } from '../../lib/format';
import { monthName, round2, str } from '../../lib/budget';
import { parsePlan } from './GroupCard';

/*
 * First-time setup (design "First-time setup"): a few rough amounts become the first plan.
 * The server does the work in one step (POST /api/budgets/setup); see services/budget_setup.py.
 */

const dollars = (v: number) => formatMoney(v, { cents: Math.abs(v - Math.round(v)) >= 0.005 });
/** "Entertainment", "Entertainment and Shopping", "A, B and C" */
const joinNames = (names: string[]) => (names.length < 2 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`);

/** `onSubmit` makes the plan; it resolves to an error message, or null once done. */
export function SetupCard({ onSubmit }: { onSubmit: (body: BudgetSetupInput) => Promise<string | null> }) {
  const uid = useId();
  const [info, setInfo] = useState<BudgetSetupInfo | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [income, setIncome] = useState('');
  const [keep, setKeep] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.budgets
      .setupInfo()
      .then((i) => {
        if (!live) return;
        setInfo(i);
        setAnswers(Object.fromEntries(i.rows.map((r) => [r.key, str(r.suggested)])));
        setIncome(i.income.start !== null ? str(i.income.start) : '');
      })
      .catch((e: unknown) => live && setLoadErr(errorMessage(e)));
    return () => {
      live = false;
    };
  }, []);

  if (!info) {
    return (
      <section className="bud-setup" aria-busy={!loadErr}>
        {loadErr ? <p className="bud-form-hint is-warn">{loadErr}</p> : <SkeletonRows rows={5} label="Loading setup" />}
      </section>
    );
  }

  const name = monthName(info.month);
  const values = info.rows.map((r) => parsePlan(answers[r.key] ?? '') ?? 0);
  const total = round2(values.reduce((a, v) => a + v, 0));
  const incomeValue = parsePlan(income);
  const incomeOk = incomeValue !== null && incomeValue + 0.004 >= info.income.received;
  // Card debt larger than the cash comes out of this month's income first.
  const owed = info.owed_beyond_cash > 0.004 ? info.owed_beyond_cash : 0;
  const limit = incomeValue === null ? null : Math.max(0, round2(incomeValue - owed));
  const over = limit !== null && total > limit + 0.004;
  const badRow = info.rows.some((r) => (answers[r.key] ?? '').trim() !== '' && parsePlan(answers[r.key] ?? '') === null);
  const canFinish = incomeOk && !over && !badRow && !busy;

  let summary: string;
  if (incomeValue === null) summary = `That’s ${dollars(total)} a month. Type about how much comes in each month to continue.`;
  else if (owed && limit !== null) summary = over
    ? `That’s ${dollars(total)} a month, ${dollars(round2(total - limit))} more than you can plan. Lower an amount to continue.`
    : `That’s ${dollars(total)} a month, so ${dollars(round2(limit - total))} would be left to plan or save.`;
  else if (over) summary = `That’s ${dollars(total)} a month. About ${dollars(incomeValue)} comes in each month, so this is ${dollars(round2(total - incomeValue))} more than comes in. Lower an amount to continue.`;
  else summary = `That’s ${dollars(total)} a month. About ${dollars(incomeValue)} comes in each month, so ${dollars(round2(incomeValue - total))} would be left to plan or save.`;

  async function submit(skip: boolean) {
    if (!info || busy) return;
    if (incomeValue === null || !incomeOk) {
      setError(incomeValue === null ? 'Type about how much comes in each month.' : `${dollars(info.income.received)} has already come in this month, so pick at least that.`);
      return;
    }
    const body: BudgetSetupInput = {
      answers: skip ? {} : (Object.fromEntries(info.rows.map((r, i) => [r.key, values[i]!])) as Record<BudgetSetupKey, number>),
      income_expected: incomeValue,
      keep_existing_in_savings: keep,
      skip,
    };
    setBusy(true);
    setError(null);
    const err = await onSubmit(body);
    setBusy(false);
    if (err) setError(err);
  }

  return (
    <section className="bud-setup" aria-labelledby={`${uid}-title`}>
      <div>
        <div className="bud-kicker">First-time setup · about 2 minutes</div>
        <h2 id={`${uid}-title`} className="bud-setup-title">
          Let’s make your first plan
        </h2>
        <p className="bud-setup-lead">About how much do you spend each month on these? A rough guess is fine. You can change any amount later.</p>
      </div>

      <div className="bud-setup-row is-income">
        <div className="bud-setup-label">
          <label htmlFor={`${uid}-income`}>Money coming in each month</label>
          <span>
            {info.income.suggested !== null
              ? `About ${dollars(info.income.suggested)} came in each month lately.`
              : 'Your pay and any other money that comes in.'}
            {info.income.received > 0.004 && ` ${dollars(info.income.received)} has come in so far this month.`}
            {info.income.expected_more > 0.004 && ` ${dollars(info.income.expected_more)} more is on your calendar for ${name}.`}
          </span>
        </div>
        <span className="bud-money is-lg">
          <span aria-hidden="true">$</span>
          <input
            id={`${uid}-income`}
            className="num"
            inputMode="decimal"
            autoComplete="off"
            value={income}
            onChange={(e) => {
              setIncome(e.target.value);
              setError(null);
            }}
            aria-invalid={(income !== '' && !incomeOk) || undefined}
          />
        </span>
      </div>

      <div className="bud-setup-rows">
        {info.rows.map((r) => {
          const v = parsePlan(answers[r.key] ?? '');
          return (
            <div className="bud-setup-row" key={r.key}>
              <div className="bud-setup-label">
                <label htmlFor={`${uid}-${r.key}`}>{r.label}</label>
                <span>
                  {r.key === 'bills' && info.bills
                    ? `We found ${dollars(info.bills.total)} in bills on your calendar this month (${info.bills.count === 1 ? '1 bill' : `${info.bills.count} bills`}).` +
                      (info.bills.extra > 0.004 ? ` The suggestion also covers about ${dollars(info.bills.extra)} of other spending in ${joinNames(info.bills.extra_categories)}.` : '')
                    : r.average !== null
                      ? `${r.hint}. About ${dollars(r.average)} a month lately.`
                      : r.hint}
                </span>
              </div>
              <div className="bud-setup-picks">
                {r.chips.map((c) => (
                  <button
                    key={c}
                    type="button"
                    className="bud-chip"
                    aria-pressed={v !== null && Math.abs(v - c) < 0.005}
                    onClick={() => setAnswers((a) => ({ ...a, [r.key]: str(c) }))}
                    aria-label={`${r.label}: ${dollars(c)}`}
                  >
                    {dollars(c)}
                  </button>
                ))}
                <span className="bud-money is-lg">
                  <span aria-hidden="true">$</span>
                  <input
                    id={`${uid}-${r.key}`}
                    className="num"
                    inputMode="decimal"
                    autoComplete="off"
                    aria-label={`${r.label} per month`}
                    value={answers[r.key] ?? ''}
                    onChange={(e) => setAnswers((a) => ({ ...a, [r.key]: e.target.value }))}
                    aria-invalid={((answers[r.key] ?? '').trim() !== '' && v === null) || undefined}
                  />
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {info.existing_money > 0.004 && (
        <div className="bud-setup-keep">
          <Icon name="info" />
          <span>
            {keep
              ? `The ${dollars(info.existing_money)} you already had on ${name} 1 stays in Emergency savings.`
              : `The ${dollars(info.existing_money)} you already had on ${name} 1 will be added to ${name}’s money.`}
          </span>
          <button type="button" className="bud-link-btn" onClick={() => setKeep(!keep)}>
            {keep ? 'Use it this month instead' : 'Keep it in Emergency savings'}
          </button>
        </div>
      )}

      {owed > 0 && incomeValue !== null && (
        <div className="bud-setup-keep">
          <Icon name="info" />
          <span>{`Your cards owe ${dollars(owed)} more than your accounts hold, so plan at most ${dollars(limit ?? 0)}.`}</span>
        </div>
      )}

      <div className="bud-setup-sum">
        <p aria-live="polite">{summary}</p>
        <button type="button" className="bud-btn bud-btn-primary bud-btn-xl" onClick={() => void submit(false)} disabled={!canFinish}>
          {busy ? 'Making your plan…' : 'Make my plan'}
          <Icon name="arrow-right" />
        </button>
      </div>
      {error && (
        <p className="bud-form-hint is-warn" role="alert">
          {error}
        </p>
      )}
      <button type="button" className="bud-link-btn bud-setup-skip" onClick={() => void submit(true)} disabled={busy}>
        Skip for now and set it up myself
      </button>
    </section>
  );
}
