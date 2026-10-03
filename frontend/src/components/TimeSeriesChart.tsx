import { useId, useMemo, type ReactNode } from 'react';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipProps } from 'recharts';
import { formatDate, formatMoney, parseISODate } from '../lib/format';

export interface SeriesPoint {
  date: string;
  value: number;
  /** Release 3: reconstructed from transactions rather than a recorded balance. */
  estimated?: boolean;
}

/** A point on the plotted rows: `real` / `est` split the line so the estimated stretch draws dashed. */
type Row<P> = P & { real: number | null; est: number | null };

const monthFmt = new Intl.DateTimeFormat(undefined, { month: 'short' });
const monthDayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const monthYearFmt = new Intl.DateTimeFormat(undefined, { month: 'short', year: '2-digit' });

/**
 * Single-series area chart: gradient fill, 2px line, recessive grid,
 * crosshair + custom tooltip. No legend (the surrounding title names it),
 * except when some points are estimated: that stretch draws dashed over a
 * lighter fill, with a small key under the chart.
 */
export function TimeSeriesChart<P extends SeriesPoint>({
  data,
  height = '100%',
  renderTooltip,
  currency = 'USD',
  ariaLabel,
  tone = 'accent',
}: {
  data: P[];
  height?: number | string;
  renderTooltip?: (p: P) => ReactNode;
  currency?: string;
  ariaLabel: string;
  tone?: 'accent' | 'liab';
}) {
  const gradId = `grad-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const spanDays = useMemo(() => {
    if (data.length < 2) return 0;
    return (parseISODate(data[data.length - 1]!.date).getTime() - parseISODate(data[0]!.date).getTime()) / 86400000;
  }, [data]);

  const tickFormatter = (iso: string) => {
    const d = parseISODate(iso);
    if (spanDays <= 100) return monthDayFmt.format(d);
    if (spanDays <= 400) return monthFmt.format(d);
    return monthYearFmt.format(d);
  };

  // Round the y-domain to "nice" steps so axis labels read $340K, $360K… not $340.3K.
  const { domain, ticks } = useMemo(() => {
    if (!data.length) return { domain: [0, 1] as [number, number], ticks: undefined };
    let min = Infinity;
    let max = -Infinity;
    for (const p of data) {
      min = Math.min(min, p.value);
      max = Math.max(max, p.value);
    }
    const span = Math.max(max - min, Math.abs(max) * 0.02, 1);
    const step = niceStep(span / 3);
    let lo = Math.floor((min - span * 0.08) / step) * step;
    if (min >= 0 && lo < 0) lo = 0;
    const hi = Math.ceil((max + span * 0.04) / step) * step;
    const ticks: number[] = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v * 100) / 100);
    return { domain: [lo, hi] as [number, number], ticks };
  }, [data]);

  const stroke = tone === 'liab' ? 'var(--liab)' : 'var(--chart-line)';

  // Estimated points: the dashed series covers every estimated point plus its real neighbours,
  // so the two strokes meet; the solid series covers the recorded points.
  const { rows, hasEst, estUntil } = useMemo(() => {
    const est = (i: number) => !!data[i]?.estimated;
    let last: string | null = null;
    const rows: Row<P>[] = data.map((p, i) => {
      if (p.estimated) last = p.date;
      return { ...p, real: p.estimated ? null : p.value, est: est(i) || est(i - 1) || est(i + 1) ? p.value : null };
    });
    return { rows, hasEst: last !== null, estUntil: last as string | null };
  }, [data]);
  const label = hasEst && estUntil ? `${ariaLabel}. Values up to ${formatDate(estUntil)} are estimated from transactions.` : ariaLabel;

  return (
    <div className={`ts-chart${hasEst ? ' has-est' : ''}`} style={{ width: '100%', height }}>
      <div role="img" aria-label={label} className="ts-chart-plot">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: 4 }}>
            <defs>
              <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" style={{ stopColor: stroke, stopOpacity: tone === 'liab' ? 0.22 : 0.26 }} />
                <stop offset="100%" style={{ stopColor: stroke, stopOpacity: 0 }} />
              </linearGradient>
              <linearGradient id={`${gradId}-est`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" style={{ stopColor: stroke, stopOpacity: 0.1 }} />
                <stop offset="100%" style={{ stopColor: stroke, stopOpacity: 0 }} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} className="chart-grid" />
            <XAxis
              dataKey="date"
              tickFormatter={tickFormatter}
              axisLine={false}
              tickLine={false}
              minTickGap={40}
              tickMargin={8}
              className="chart-axis"
            />
            <YAxis
              domain={domain}
              tickFormatter={(v: number) => formatMoney(v, { currency, compact: true })}
              axisLine={false}
              tickLine={false}
              width={60}
              ticks={ticks}
              className="chart-axis"
            />
            <Tooltip
              cursor={{ className: 'chart-cursor', strokeDasharray: '3 3' }}
              content={(props: TooltipProps<number, string>) => {
                const p = props.payload?.[0]?.payload as P | undefined;
                if (!props.active || !p) return null;
                return (
                  <div className="chart-tip">
                    <div className="chart-tip-date">
                      {formatDate(p.date)}
                      {p.estimated && <span className="chart-tip-est"> · estimated</span>}
                    </div>
                    {renderTooltip ? renderTooltip(p) : <div className="chart-tip-main num">{formatMoney(p.value, { currency })}</div>}
                  </div>
                );
              }}
              isAnimationActive={false}
            />
            {hasEst && (
              <Area
                type="monotone"
                dataKey="est"
                stroke={stroke}
                strokeWidth={2}
                strokeDasharray="5 4"
                strokeOpacity={0.75}
                fill={`url(#${gradId}-est)`}
                className="chart-area chart-area-est"
                activeDot={{ r: 4.5, strokeWidth: 2, className: 'chart-dot' }}
                dot={false}
                fillOpacity={1}
                connectNulls={false}
                isAnimationActive={false}
              />
            )}
            <Area
              type="monotone"
              dataKey={hasEst ? 'real' : 'value'}
              stroke={stroke}
              strokeWidth={2}
              fill={`url(#${gradId})`}
              className={tone === 'liab' ? 'chart-area chart-area-liab' : 'chart-area'}
              activeDot={{ r: 4.5, strokeWidth: 2, className: 'chart-dot' }}
              dot={false}
              fillOpacity={1}
              connectNulls={false}
              // Product UI loads straight into the data; no draw-on animation.
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {hasEst && (
        <div className="ts-legend">
          <span className="ts-key">
            <span className={`ts-swatch${tone === 'liab' ? ' is-liab' : ''}`} aria-hidden="true" />
            Recorded
          </span>
          <span className="ts-key">
            <span className={`ts-swatch is-est${tone === 'liab' ? ' is-liab' : ''}`} aria-hidden="true" />
            Estimated from transactions
          </span>
        </div>
      )}
    </div>
  );
}

function niceStep(raw: number): number {
  const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nice * exp;
}
