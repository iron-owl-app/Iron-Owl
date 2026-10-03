import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type DayTotals, type Transaction, type TransactionFilters, type TransactionSummary } from '../../api';
import { useApp } from '../../state';

const FIRST_PAGE = 40;
const NEXT_PAGE = 30;
/** Largest page the API serves; reloads chain pages of this size by cursor. */
const MAX_PAGE = 500;

interface PagesData {
  items: Transaction[];
  days: Record<string, DayTotals>;
  total: number;
  nextCursor: string | null;
  /** The first page for the current filters has arrived. */
  loaded: boolean;
}

const EMPTY: PagesData = { items: [], days: {}, total: 0, nextCursor: null, loaded: false };

export interface TxnPages extends PagesData {
  /** A request is in flight (first page, more, or a reload). */
  loading: boolean;
  /** The last request failed. */
  error: unknown;
  /** A new first page is loading while the previous filters' rows are still shown. */
  refreshing: boolean;
  /** Bumped when a fresh first page replaces the list (filters or data changed): scroll to top. */
  generation: number;
  loadMore: () => void;
  /** Swap in updated rows (unknown ids are ignored). Rows stay where they are, and win over a reload in flight. */
  replace: (rows: Transaction[]) => void;
  /** Re-fetch everything loaded so far from the top (after rules change), keeping the scroll position. */
  reloadLoaded: () => Promise<void>;
  retry: () => void;
}

/**
 * Infinite list of transactions for a filter set, by keyset cursor: 40 rows first, then 30
 * at a time. Resets when the filters (`key`) or global data (`dataVersion`) change; while a
 * new first page loads, the old rows stay (dimmed by the caller). Stale responses are dropped.
 */
export function useTxnPages(filters: TransactionFilters, key: string): TxnPages {
  const { dataVersion } = useApp();
  const [data, setData] = useState<PagesData>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [generation, setGeneration] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const gen = useRef(0);
  const busy = useRef(false);
  const dataRef = useRef(data);
  dataRef.current = data;
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const replacedDuringReload = useRef<{ rows: Map<number, Transaction> } | null>(null);

  const begin = () => {
    busy.current = true;
    setLoading(true);
    setError(null);
  };
  const finish = (g: number) => {
    if (g !== gen.current) return;
    busy.current = false;
    setLoading(false);
    setRefreshing(false);
  };

  const loadFirst = useCallback(() => {
    const g = ++gen.current;
    begin();
    setRefreshing(true);
    api
      .transactions({ ...filtersRef.current, limit: FIRST_PAGE })
      .then((p) => {
        if (g !== gen.current) return;
        setData({ items: p.items, days: p.days, total: p.total, nextCursor: p.next_cursor, loaded: true });
        setGeneration((n) => n + 1);
      })
      .catch((e: unknown) => {
        if (g !== gen.current) return;
        // Don't leave the previous filters' rows up: show the error, and Retry starts over.
        setData(EMPTY);
        setError(e);
      })
      .finally(() => finish(g));
  }, []);

  useEffect(() => {
    loadFirst();
  }, [key, dataVersion, loadFirst]);

  const loadMore = useCallback(() => {
    const d = dataRef.current;
    if (busy.current || !d.loaded || !d.nextCursor) return;
    const g = gen.current;
    begin();
    api
      .transactions({ ...filtersRef.current, limit: NEXT_PAGE, cursor: d.nextCursor })
      .then((p) => {
        if (g !== gen.current) return;
        setData((prev) => {
          const seen = new Set(prev.items.map((t) => t.id));
          return {
            ...prev,
            items: [...prev.items, ...p.items.filter((t) => !seen.has(t.id))],
            days: { ...prev.days, ...p.days },
            total: p.total,
            nextCursor: p.next_cursor,
          };
        });
      })
      .catch((e: unknown) => g === gen.current && setError(e))
      .finally(() => finish(g));
  }, []);

  const replace = useCallback((rows: Transaction[]) => {
    if (!rows.length) return;
    const byId = new Map(rows.map((t) => [t.id, t]));
    for (const t of rows) replacedDuringReload.current?.rows.set(t.id, t);
    setData((prev) => (prev.items.some((t) => byId.has(t.id)) ? { ...prev, items: prev.items.map((t) => byId.get(t.id) ?? t) } : prev));
  }, []);

  const reloadLoaded = useCallback(async () => {
    const want = Math.max(FIRST_PAGE, dataRef.current.items.length);
    const g = ++gen.current;
    // Rows edited while this reload runs are newer than what it fetches: they win.
    const replaced = { rows: new Map<number, Transaction>() };
    replacedDuringReload.current = replaced;
    begin();
    try {
      const items: Transaction[] = [];
      const days: Record<string, DayTotals> = {};
      let cursor: string | undefined;
      let next: string | null = null;
      let total = 0;
      while (items.length < want) {
        const p = await api.transactions({ ...filtersRef.current, limit: Math.min(MAX_PAGE, want - items.length), cursor });
        if (g !== gen.current) return;
        items.push(...p.items);
        Object.assign(days, p.days);
        total = p.total;
        next = p.next_cursor;
        if (!next) break;
        cursor = next;
      }
      // "Needs a category" keeps rows in place until the filters change: rows the change took
      // out of the view are fetched again (fresh) from the same filters without the view.
      const kept = await keptOutOfView(filtersRef.current, dataRef.current.items, items);
      if (g !== gen.current) return;
      if (kept.length) {
        items.push(...kept);
        items.sort((a, b) => (a.date === b.date ? b.id - a.id : a.date < b.date ? 1 : -1));
      }
      const prevDays = kept.length ? dataRef.current.days : {};
      setData({
        items: items.map((t) => replaced.rows.get(t.id) ?? t),
        days: { ...prevDays, ...days },
        total,
        nextCursor: next,
        loaded: true,
      });
    } catch (e) {
      if (g === gen.current) setError(e);
    } finally {
      if (replacedDuringReload.current === replaced) replacedDuringReload.current = null;
      finish(g);
    }
  }, []);

  const retry = useCallback(() => {
    if (dataRef.current.loaded) loadMore();
    else loadFirst();
  }, [loadFirst, loadMore]);

  return { ...data, loading, error, refreshing, generation, loadMore, replace, reloadLoaded, retry };
}

/** Loaded rows of the "Needs a category" view that a reload no longer returns, re-fetched fresh. */
async function keptOutOfView(filters: TransactionFilters, loaded: Transaction[], fresh: Transaction[]): Promise<Transaction[]> {
  if (filters.view !== 'needs_category') return [];
  const freshIds = new Set(fresh.map((t) => t.id));
  const missing = loaded.filter((t) => !freshIds.has(t.id));
  if (!missing.length) return [];
  const want = new Set(missing.map((t) => t.id));
  const dates = missing.map((t) => t.date).sort();
  const first = dates[0]!;
  const last = dates[dates.length - 1]!;
  const start = filters.start && filters.start > first ? filters.start : first;
  const end = filters.end && filters.end < last ? filters.end : last;
  const found: Transaction[] = [];
  let cursor: string | undefined;
  for (;;) {
    const p = await api.transactions({ ...filters, view: undefined, start, end, limit: MAX_PAGE, cursor });
    for (const t of p.items) if (want.has(t.id)) found.push(t);
    if (!p.next_cursor || found.length === want.size) break;
    cursor = p.next_cursor;
  }
  return found;
}

/** Header, pills and the no-selection panel. `reload()` is debounced (150 ms) for bursts of edits. */
export function useTxnSummary(filters: TransactionFilters, key: string) {
  const { dataVersion } = useApp();
  const [state, setState] = useState<{ data: TransactionSummary | null; key: string | null }>({ data: null, key: null });
  const gen = useRef(0);
  const timer = useRef<number | undefined>(undefined);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const keyRef = useRef(key);
  keyRef.current = key;

  const load = useCallback(() => {
    const g = ++gen.current;
    const k = keyRef.current;
    api
      .transactionsSummary(filtersRef.current)
      .then((d) => g === gen.current && setState({ data: d, key: k }))
      .catch(() => {
        /* the list shows the error; the header just keeps its last numbers */
      });
  }, []);

  useEffect(() => {
    window.clearTimeout(timer.current);
    load();
  }, [key, dataVersion, load]);

  const reload = useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(load, 150);
  }, [load]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  /** `key`: the filters `data` is for (it lags behind while a new summary loads). */
  return { data: state.data, key: state.key, reload };
}
