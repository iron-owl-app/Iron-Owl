import { useMemo } from 'react';
import { api, type Account, type Holding } from '../../api';
import { useApi } from '../../lib/useApi';
import { CATEGORY_LABEL, formatMoney, formatPercent, formatQuantity, plural } from '../../lib/format';
import { CategoryChip } from '../../components/Icon';
import { Avatar, Money, Skeleton, SkeletonRows } from '../../components/ui';
import { ErrorPanel } from '../../components/ErrorPanel';

/*
 * Investments' "Detailed view": the earlier holdings tables (ticker, quantity, price, cost basis,
 * gain or loss per holding), kept for the owner behind the switch. Same data as before
 * (GET /api/holdings and /api/accounts).
 */

interface Group {
  accountId: number;
  name: string;
  account: Account | undefined;
  holdings: Holding[];
  value: number;
  basis: number;
  /** Value of the holdings that have a cost basis (for gain %). */
  valueWithBasis: number;
}

function gainOf(value: number, basis: number) {
  const gain = value - basis;
  return { gain, pct: basis > 0.005 ? gain / basis : null };
}

export function InvestmentsDetailed() {
  const holdings = useApi(() => api.holdings(), []);
  const accounts = useApi(() => api.accounts.list(), []);

  const { groups, others, totals } = useMemo(() => {
    const byId = new Map((accounts.data ?? []).map((a) => [a.id, a]));
    const map = new Map<number, Group>();
    for (const h of holdings.data ?? []) {
      let g = map.get(h.account_id);
      if (!g) {
        g = { accountId: h.account_id, name: h.account_name, account: byId.get(h.account_id), holdings: [], value: 0, basis: 0, valueWithBasis: 0 };
        map.set(h.account_id, g);
      }
      g.holdings.push(h);
      g.value += h.value;
      if (h.cost_basis !== null) {
        g.basis += h.cost_basis;
        g.valueWithBasis += h.value;
      }
    }
    const groups = [...map.values()]
      .filter((g) => !g.account?.hidden)
      .sort((a, b) => b.value - a.value)
      .map((g) => ({ ...g, holdings: [...g.holdings].sort((x, y) => y.value - x.value) }));
    const others = (accounts.data ?? [])
      .filter((a) => !a.hidden && (a.category === 'investment' || a.category === 'retirement' || a.category === 'hsa') && !map.has(a.id))
      .sort((a, b) => b.current_balance - a.current_balance);
    const totals = groups.reduce(
      (t, g) => ({ value: t.value + g.value, basis: t.basis + g.basis, valueWithBasis: t.valueWithBasis + g.valueWithBasis }),
      { value: 0, basis: 0, valueWithBasis: 0 },
    );
    return { groups, others, totals };
  }, [holdings.data, accounts.data]);

  if (holdings.error && !holdings.data) return <ErrorPanel error={holdings.error} onRetry={holdings.reload} />;

  const loading = (holdings.loading && !holdings.data) || (accounts.loading && !accounts.data);
  const total = gainOf(totals.valueWithBasis, totals.basis);
  const otherValue = others.reduce((s, a) => s + a.current_balance, 0);

  if (loading) {
    return (
      <>
        <dl className="stat-strip" aria-hidden="true">
          {Array.from({ length: 3 }, (_, i) => (
            <div className="stat" key={i}>
              <Skeleton width={80} height={10} />
              <Skeleton width={120} height={20} style={{ marginTop: 8 }} />
            </div>
          ))}
        </dl>
        <div className="panel">
          <SkeletonRows rows={5} label="Loading holdings" />
        </div>
      </>
    );
  }

  return (
    <div className="inv-detailed">
      <dl className="stat-strip">
        <div className="stat">
          <dt>Total value</dt>
          <dd>
            <Money value={totals.value + otherValue} />
          </dd>
        </div>
        <div className="stat">
          <dt>Cost basis</dt>
          <dd>{totals.basis > 0 ? <Money value={totals.basis} /> : <span className="subtle">—</span>}</dd>
        </div>
        <div className="stat">
          <dt>Unrealized gain / loss</dt>
          <dd>
            {totals.basis > 0 ? (
              <>
                <Money value={total.gain} signed tone="auto" />
                {total.pct !== null && (
                  <span className={`small num ${total.gain >= 0 ? 'gain' : 'loss'}`} style={{ marginLeft: 8, fontWeight: 550 }}>
                    {formatPercent(total.pct, true)}
                  </span>
                )}
              </>
            ) : (
              <span className="subtle">—</span>
            )}
          </dd>
        </div>
      </dl>

      {groups.map((g) => (
        <HoldingsGroup key={g.accountId} group={g} />
      ))}

      {others.length > 0 && (
        <section className="panel" aria-labelledby="other-inv">
          <div className="panel-head">
            <h2 id="other-inv">Balances without holdings detail</h2>
            <Money value={otherValue} className="group-total" />
          </div>
          <div>
            {others.map((a) => (
              <div className="row" key={a.id} style={{ gridTemplateColumns: 'minmax(0,1fr) auto' }}>
                <div className="row-main">
                  <CategoryChip category={a.category} />
                  <div style={{ minWidth: 0 }}>
                    <div className="row-title">
                      <span className="truncate">{a.name}</span>
                      {a.source === 'manual' && <span className="badge">Manual</span>}
                    </div>
                    <div className="row-sub truncate">{[a.institution_name, CATEGORY_LABEL[a.category]].filter(Boolean).join(' · ')}</div>
                  </div>
                </div>
                <Money value={a.current_balance} currency={a.currency} className="row-amount" />
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function HoldingsGroup({ group: g }: { group: Group }) {
  const gg = gainOf(g.valueWithBasis, g.basis);
  const currency = g.account?.currency ?? 'USD';
  const headId = `acct-${g.accountId}`;
  return (
    <section className="panel" aria-labelledby={headId}>
      <div className="group-head">
        <Avatar name={g.account?.institution_name || g.name} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 id={headId} className="truncate">
            {g.name}
          </h2>
          <div className="row-sub">
            {[g.account?.institution_name, g.account ? CATEGORY_LABEL[g.account.category] : null, plural(g.holdings.length, 'holding')].filter(Boolean).join(' · ')}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <Money value={g.value} currency={currency} className="group-total" />
          {g.basis > 0 && (
            <div className={`small num ${gg.gain >= 0 ? 'gain' : 'loss'}`}>
              {formatMoney(gg.gain, { currency, signed: true })}
              {gg.pct !== null && ` (${formatPercent(gg.pct, true)})`}
            </div>
          )}
        </div>
      </div>
      <div className="table-wrap">
        <table className="table holdings-table">
          <thead>
            <tr>
              <th scope="col">Holding</th>
              <th scope="col" className="r col-qty">
                Quantity
              </th>
              <th scope="col" className="r col-price">
                Price
              </th>
              <th scope="col" className="r">
                Value
              </th>
              <th scope="col" className="r col-basis">
                Cost basis
              </th>
              <th scope="col" className="r">
                Gain / loss
              </th>
            </tr>
          </thead>
          <tbody>
            {g.holdings.map((h) => {
              const hg = h.cost_basis !== null ? gainOf(h.value, h.cost_basis) : null;
              return (
                <tr key={h.id}>
                  <td>
                    <div className="cell-flex">
                      <span className="ticker">{h.ticker ?? '—'}</span>
                      <span className="truncate" style={{ maxWidth: 320 }} title={h.name}>
                        {h.name}
                      </span>
                    </div>
                  </td>
                  <td className="r num col-qty">{formatQuantity(h.quantity)}</td>
                  <td className="r num col-price">{h.price !== null ? formatMoney(h.price, { currency }) : '—'}</td>
                  <td className="r">
                    <Money value={h.value} currency={currency} />
                  </td>
                  <td className="r num col-basis">{h.cost_basis !== null ? formatMoney(h.cost_basis, { currency }) : '—'}</td>
                  <td className="r">
                    {hg ? (
                      <>
                        <Money value={hg.gain} currency={currency} signed tone="auto" />
                        {hg.pct !== null && <div className={`xsmall num ${hg.gain >= 0 ? 'gain' : 'loss'}`}>{formatPercent(hg.pct, true)}</div>}
                      </>
                    ) : (
                      <span className="subtle">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr>
              <td>Total</td>
              <td className="col-qty" />
              <td className="col-price" />
              <td className="r">
                <Money value={g.value} currency={currency} />
              </td>
              <td className="r num col-basis">{g.basis > 0 ? formatMoney(g.basis, { currency }) : '—'}</td>
              <td className="r">{g.basis > 0 ? <Money value={gg.gain} currency={currency} signed tone="auto" /> : '—'}</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}
