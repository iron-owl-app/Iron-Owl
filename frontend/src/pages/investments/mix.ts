import type { InvestmentMix, MixKind } from '../../api';

/*
 * What each kind of holding is called on the page, in plain words (design D9 "What it holds").
 * The server sorts holdings into kinds (services/investments.py); the words and colors live
 * here. Colors are oklch(L C H) with L/C from tokens (lifted in dark), hue per kind.
 */

interface Words {
  name: string;
  desc: string;
  /** Hue, or null for the low-chroma greys. */
  hue: number | null;
}

const WORDS: Record<Exclude<MixKind, 'target_date'>, Words> = {
  us_stock: { name: 'US stock fund', desc: 'Owns small pieces of hundreds of large US companies.', hue: 268 },
  intl_stock: { name: 'Stocks outside the US', desc: 'Companies in Europe, Asia and other places.', hue: 200 },
  bond: { name: 'Bond fund', desc: 'Loans to governments and companies. Goes up and down less than stocks.', hue: 155 },
  cash: { name: 'Cash', desc: 'Not invested, so it doesn’t go up or down.', hue: null },
  company_stock: { name: 'Single companies', desc: 'Shares of one company each. These can go up and down a lot.', hue: 320 },
  other: { name: 'Other investments', desc: 'Things Iron Owl can’t sort into the groups above.', hue: 110 },
  not_broken_down: { name: 'Not broken down', desc: 'Your bank doesn’t say what this part holds.', hue: null },
};

export function mixWords(x: InvestmentMix, wholeAccount = false): { name: string; desc: string } {
  if (x.kind === 'target_date') {
    return x.year
      ? { name: `Retirement date fund, ${x.year}`, desc: `A ready-made mix. It gets more careful as ${x.year} gets closer.` }
      : { name: 'Retirement date fund', desc: 'A ready-made mix. It gets more careful as retirement gets closer.' };
  }
  if (x.kind === 'not_broken_down' && wholeAccount) return { name: 'Not broken down', desc: 'Your bank doesn’t say what this holds.' };
  return WORDS[x.kind];
}

/** The segment / square color for a kind. */
export function mixColor(kind: MixKind): string {
  if (kind === 'target_date') return 'oklch(var(--inv-mix-lc) 80)';
  if (kind === 'cash') return 'var(--inv-mix-cash)';
  if (kind === 'not_broken_down') return 'var(--inv-mix-none)';
  return `oklch(var(--inv-mix-lc) ${WORDS[kind].hue ?? 262})`;
}

/** "Fidelity 500 Index Fund, Vanguard Total Stock Market ETF and 2 more" */
export function fundNames(names: string[]): string {
  const clean = names.filter((n) => n.trim());
  if (clean.length <= 3) return clean.join(', ');
  return `${clean.slice(0, 3).join(', ')} and ${clean.length - 3} more`;
}
