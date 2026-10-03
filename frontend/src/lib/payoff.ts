export type PayoffEstimate =
  | { kind: 'paid' }
  | { kind: 'missing' }
  | { kind: 'never'; monthlyInterest: number }
  | { kind: 'months'; months: number; totalInterest: number; payoffDate: Date };

// Same rules as the server's payoff projection (SPEC Release 3 "Payoff math"), so the
// loan card and the payoff view agree: whole cents, interest rounded each month, 600 months.
const MAX_MONTHS = 600;

/** round_cents(balance x apr / 100 / 12), half away from zero; balance in cents. */
function monthlyInterestCents(balanceCents: number, aprPct: number): number {
  if (balanceCents <= 0 || aprPct <= 0) return 0;
  return Math.floor((balanceCents * aprPct) / 1200 + 0.5);
}

/**
 * Months to pay off `balance` at `aprPct` (annual %, compounded monthly) paying
 * `payment` per month. "never" when the payment doesn't exceed the first month's
 * interest, or the balance isn't paid off within 600 months.
 */
export function estimatePayoff(balance: number, aprPct: number | null, payment: number | null): PayoffEstimate {
  if (balance <= 0.005) return { kind: 'paid' };
  if (aprPct === null || payment === null || !Number.isFinite(aprPct) || !Number.isFinite(payment) || payment <= 0) {
    return { kind: 'missing' };
  }
  const apr = Math.max(0, aprPct);
  const paymentCents = Math.round(payment * 100);
  let b = Math.round(balance * 100);
  const firstInterest = monthlyInterestCents(b, apr);
  const monthlyInterest = firstInterest / 100;
  if (paymentCents <= firstInterest) return { kind: 'never', monthlyInterest };

  let months = 0;
  let interestCents = 0;
  while (b > 0 && months < MAX_MONTHS) {
    const interest = monthlyInterestCents(b, apr);
    interestCents += interest;
    b = b + interest - Math.min(b + interest, paymentCents);
    months++;
  }
  if (b > 0) return { kind: 'never', monthlyInterest };
  const totalInterest = interestCents / 100;

  const payoffDate = new Date();
  payoffDate.setDate(1);
  payoffDate.setMonth(payoffDate.getMonth() + months);
  return { kind: 'months', months, totalInterest, payoffDate };
}

export function formatDuration(months: number): string {
  const y = Math.floor(months / 12);
  const m = months % 12;
  if (y === 0) return `${m} mo`;
  if (m === 0) return `${y} yr`;
  return `${y} yr ${m} mo`;
}
