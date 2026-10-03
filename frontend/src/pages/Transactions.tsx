import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, ApiError, errorMessage, saveBlob, type BulkCategoryResult, type CategoryState, type Rule, type Transaction, type TransactionFilters, type TxnCategory, type TxnView } from '../api';
import { useApi } from '../lib/useApi';
import { readPref, writePref } from '../lib/prefs';
import { plural, toISODate } from '../lib/format';
import { groupIndexMap } from '../lib/categoryColors';
import { Icon } from '../components/Icon';
import { EmptyState } from '../components/EmptyState';
import { CategoryPopover, isHandSet, type PopoverCloseReason } from '../components/CategoryPopover';
import { useToast } from '../components/Toast';
import { useLinkBankTo } from '../state';
import { SplitEditor } from '../components/SplitEditor';
import { useTxnPages, useTxnSummary } from './transactions/useTxnPages';
import { useTxnKeyboard, type TxnKey } from './transactions/useTxnKeyboard';
import { TxnHeader, TxnFilterRow } from './transactions/TxnHeader';
import { TxnToolbar } from './transactions/TxnToolbar';
import { TxnList } from './transactions/TxnList';
import type { RowHandlers } from './transactions/TxnRow';
import { TxnDetailsPanel } from './transactions/TxnDetailsPanel';
import { TxnSummaryPanel } from './transactions/TxnSummaryPanel';
import { TxnUndoToast, type UndoToast } from './transactions/TxnUndoToast';
import type { RuleOffer } from './transactions/BulkOffer';
import { MAX_RULE_TEXT, merchantKey, ruleMatchFor, ruleText, txnTitle, useMedia } from './transactions/shared';
import './transactions.css';

const VIEWS: readonly TxnView[] = ['all', 'needs_category', 'in', 'out'];
const isView = (v: string | null): v is TxnView => v !== null && (VIEWS as readonly string[]).includes(v);
const RECENT_KEY = 'tx.recentCategories';
const MAX_RECENT = 3;
const isStringArray = (v: unknown): v is string[] => Array.isArray(v) && v.every((x) => typeof x === 'string');
const NO_CATEGORIES: TxnCategory[] = [];
const NO_RULES: Rule[] = [];
/** Most ids the bulk set / restore endpoints take per request. */
const MAX_BULK = 1000;
/** Most pages one keyboard move will load while looking for the next row. */
const MAX_CHASE_PAGES = 10;

type Picker = { target: number | 'bulk'; anchor: DOMRect; viaKey: boolean; returnFocus: HTMLElement | null };
/** A keyboard move waiting for the next page: select the row after `after` (or the next one needing a category). */
type Pending = { after: number; mode: 'next' | 'needs'; focus: boolean; pages: number };
/** An "Always for…" rule being created, so Undo can wait for it and delete it. */
type AlwaysSave = { key: number; ruleId: Promise<number | null>; undone: boolean };

const isNeeding = (t: Transaction) => t.needs_category && !t.splits.length;

/** Ids in requests of at most MAX_BULK. */
function chunks<T>(xs: T[]): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < xs.length; i += MAX_BULK) out.push(xs.slice(i, i + MAX_BULK));
  return out;
}

const focusEl = (el: HTMLElement | null | undefined) => {
  if (el && el.isConnected) el.focus({ preventScroll: true });
};
const rowName = (id: number) => document.querySelector<HTMLElement>(`.txn-list [data-tx="${id}"] [data-name]`);
const rowChip = (id: number) => document.querySelector<HTMLElement>(`.txn-list [data-tx="${id}"] [data-chip]`);

export function TransactionsPage() {
  const toast = useToast();
  const linkTo = useLinkBankTo();
  const [params, setParams] = useSearchParams();
  const accountParam = Number(params.get('account') ?? '');
  const accountId = Number.isInteger(accountParam) && accountParam > 0 ? accountParam : undefined;
  const start = params.get('start') ?? '';
  const end = params.get('end') ?? '';
  const search = params.get('q') ?? '';
  const category = params.get('cat') ?? '';
  const tagParam = Number(params.get('tag') ?? '');
  const tag = Number.isInteger(tagParam) && tagParam > 0 ? tagParam : undefined;
  const viewParam = params.get('view');
  const view: TxnView = isView(viewParam) ? viewParam : 'all';

  const filters = useMemo<TransactionFilters>(
    () => ({ account_id: accountId, search: search || undefined, start: start || undefined, end: end || undefined, category: category || undefined, tag, view }),
    [accountId, search, start, end, category, tag, view],
  );
  const filterKey = JSON.stringify(filters);
  const filterCount = [accountId, category, tag, start, end].filter(Boolean).length;
  const unfiltered = !search && filterCount === 0 && view === 'all';

  const pages = useTxnPages(filters, filterKey);
  const summary = useTxnSummary(filters, filterKey);
  const accounts = useApi(() => api.accounts.list(), []);
  const cats = useApi(() => api.categories.list(), []);
  const groupList = useApi(() => api.categoryGroups.list(), []);
  const tagList = useApi(() => api.tags.list(), []);
  const ruleList = useApi(() => api.rules.list(), []);

  const categories = cats.data ?? NO_CATEGORIES;
  const groups = useMemo(() => groupList.data ?? [], [groupList.data]);
  const rules = ruleList.data ?? NO_RULES;
  const catById = useMemo(() => new Map(categories.map((c) => [c.id, c])), [categories]);
  const groupIndex = useMemo(() => groupIndexMap(groups), [groups]);
  const groupNames = useMemo(() => new Map(groups.map((g) => [g.id, g.name])), [groups]);
  const accountLabels = useMemo(() => new Map((accounts.data ?? []).map((a) => [a.id, a.mask ? `${a.name} ··${a.mask}` : a.name])), [accounts.data]);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [checked, setChecked] = useState<ReadonlySet<number>>(() => new Set());
  const [picker, setPicker] = useState<Picker | null>(null);
  const [recent, setRecent] = useState<string[]>(() => readPref(RECENT_KEY, [], isStringArray).slice(0, MAX_RECENT));
  const [undo, setUndo] = useState<UndoToast | null>(null);
  const [splitFor, setSplitFor] = useState<Transaction | null>(null);
  const [offer, setOffer] = useState<RuleOffer | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(filterCount > 0);
  const [exporting, setExporting] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const pageScroll = useMedia('(max-width: 860px)');

  const listRef = useRef<HTMLElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const sideRef = useRef<HTMLElement>(null);
  const splitReturn = useRef<HTMLElement | null>(null);
  const pending = useRef<Pending | null>(null);
  const toastKey = useRef(0);
  // Latest values for stable callbacks (rows are memoized on stable handlers).
  const itemsRef = useRef(pages.items);
  itemsRef.current = pages.items;
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const checkedRef = useRef(checked);
  checkedRef.current = checked;
  const undoRef = useRef(undo);
  undoRef.current = undo;
  const pageScrollRef = useRef(pageScroll);
  pageScrollRef.current = pageScroll;
  const filterKeyRef = useRef(filterKey);
  filterKeyRef.current = filterKey;
  const setParamsRef = useRef(setParams);
  setParamsRef.current = setParams;
  // How many rows need a category under these filters (null until the summary for them is in).
  const needsTotalRef = useRef<number | null>(null);
  needsTotalRef.current = summary.key === filterKey && summary.data ? summary.data.needs_category : null;
  const alwaysSave = useRef<AlwaysSave | null>(null);

  const findTxn = (id: number) => itemsRef.current.find((t) => t.id === id);
  const selected = selectedId === null ? null : (pages.items.find((t) => t.id === selectedId) ?? null);

  // ---------------------------------------------------------------- URL filters

  const [searchDraft, setSearchDraft] = useState(search);
  useEffect(() => setSearchDraft(search), [search]);
  useEffect(() => {
    if (searchDraft === search) return;
    const h = window.setTimeout(() => update({ q: searchDraft || null }), 300);
    return () => window.clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft]);

  /**
   * Patch the URL filters. Goes through the latest render's setter (it builds on that render's
   * params), so the debounced search write can't undo a pill or filter picked in the meantime.
   */
  function update(patch: Record<string, string | null>) {
    setParamsRef.current(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('page'); // old paginated links
        for (const [k, v] of Object.entries(patch)) {
          if (v === null || v === '') next.delete(k);
          else next.set(k, v);
        }
        return next;
      },
      { replace: true },
    );
  }
  const clearFilters = () => update({ account: null, cat: null, tag: null, start: null, end: null });
  const clearAll = () => setParams(new URLSearchParams(), { replace: true });

  // New filters: nothing selected, ticked or open.
  useEffect(() => {
    setSelectedId(null);
    setChecked(new Set());
    setPicker(null);
    setOffer(null);
    pending.current = null;
  }, [filterKey]);

  // A deep link or a change elsewhere set a filter: show the filter row.
  useEffect(() => {
    if (filterCount > 0) setFiltersOpen(true);
  }, [filterCount]);

  // A fresh first page starts at the top.
  useEffect(() => {
    if (listRef.current && !pageScrollRef.current) listRef.current.scrollTop = 0;
  }, [pages.generation]);

  // The selected row went away (a reload after a rule change): close the panel.
  useEffect(() => {
    if (selectedId !== null && pages.loaded && !pages.refreshing && !pages.items.some((t) => t.id === selectedId)) setSelectedId(null);
  }, [pages.items, pages.loaded, pages.refreshing, selectedId]);

  // ---------------------------------------------------------------- layout

  const [listTop, setListTop] = useState(244);
  const [stickyTop, setStickyTop] = useState(0);
  useLayoutEffect(() => {
    const measure = () => {
      const el = listRef.current;
      if (el) setListTop(Math.round(el.getBoundingClientRect().top + window.scrollY));
      const bar = document.querySelector<HTMLElement>('.topbar');
      setStickyTop(bar && getComputedStyle(bar).display !== 'none' ? bar.offsetHeight : 0);
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (headRef.current) ro.observe(headRef.current);
    window.addEventListener('resize', measure);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, []);

  /** Bring a row into view by adjusting scrollTop (the list, or the page on phones), clear of the sticky day header. */
  const scrollRowIntoView = useCallback((id: number) => {
    const row = document.querySelector<HTMLElement>(`.txn-list [data-tx="${id}"]`);
    if (!row) return;
    const headH = row.closest('.txn-day')?.querySelector<HTMLElement>('.txn-day-head')?.offsetHeight ?? 40;
    const r = row.getBoundingClientRect();
    if (pageScrollRef.current) {
      const top = (document.querySelector<HTMLElement>('.topbar')?.offsetHeight ?? 0) + headH;
      const bottom = window.innerHeight - (document.querySelector<HTMLElement>('.tabbar')?.offsetHeight ?? 0);
      if (r.top < top) window.scrollBy(0, r.top - top);
      else if (r.bottom > bottom - 8) window.scrollBy(0, r.bottom - bottom + 8);
      return;
    }
    const box = listRef.current;
    if (!box) return;
    const b = box.getBoundingClientRect();
    if (r.top < b.top + headH) box.scrollTop -= b.top + headH - r.top;
    else if (r.bottom > b.bottom - 8) box.scrollTop += r.bottom - b.bottom + 8;
  }, []);

  /** Select a row from the keyboard: scroll to it; move focus along if focus was on a row. */
  const selectRow = useCallback(
    (id: number, focus: boolean | 'auto' = 'auto') => {
      const move = focus === 'auto' ? !!listRef.current?.contains(document.activeElement) : focus;
      setSelectedId(id);
      requestAnimationFrame(() => {
        scrollRowIntoView(id);
        if (move) focusEl(rowName(id));
      });
    },
    [scrollRowIntoView],
  );

  /** Next row needing a category after `afterId`, in loaded order, wrapping around. */
  const nextNeeding = (afterId: number) => {
    const items = itemsRef.current;
    const i = items.findIndex((t) => t.id === afterId);
    const order = i < 0 ? items : [...items.slice(i + 1), ...items.slice(0, i)];
    return order.find((t) => t.id !== afterId && isNeeding(t)) ?? null;
  };

  /** Rows needing a category exist beyond those loaded (per the summary; assumed while it's unknown). */
  const needingUnloaded = () => {
    const total = needsTotalRef.current;
    return total === null || total > itemsRef.current.filter(isNeeding).length;
  };

  function selectNextNeeding(afterId: number, focus: boolean) {
    const next = nextNeeding(afterId);
    if (next) selectRow(next.id, focus);
    else if (pages.nextCursor && needingUnloaded()) {
      pending.current = { after: afterId, mode: 'needs', focus, pages: 1 };
      pages.loadMore();
    }
  }

  // Finish a keyboard move that had to load the next page first.
  useEffect(() => {
    const p = pending.current;
    if (!p || pages.loading) return;
    if (pages.error) {
      pending.current = null;
      return;
    }
    const items = pages.items;
    const at = items.findIndex((t) => t.id === p.after);
    if (p.mode === 'next' && at < 0) {
      pending.current = null; // the row went away (a reload): nothing to move from
      return;
    }
    const found = p.mode === 'next' ? items[at + 1] : nextNeeding(p.after);
    if (found) {
      pending.current = null;
      selectRow(found.id, p.focus);
    } else if (pages.nextCursor && p.pages < MAX_CHASE_PAGES && (p.mode === 'next' || needingUnloaded())) {
      p.pages += 1;
      pages.loadMore();
    } else {
      pending.current = null;
      // Gave up: focus left with the picker, so put it back on the row.
      if (p.focus && (!document.activeElement || document.activeElement === document.body)) focusEl(rowName(p.after));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pages.items, pages.loading, pages.nextCursor, pages.error]);

  // ---------------------------------------------------------------- mutations

  function pushRecent(id: string) {
    setRecent((prev) => {
      const next = [id, ...prev.filter((x) => x !== id)].slice(0, MAX_RECENT);
      writePref(RECENT_KEY, next);
      return next;
    });
  }

  const dismissUndo = useCallback(() => setUndo(null), []);
  function showUndo(t: Omit<UndoToast, 'key' | 'alwaysState'>) {
    setUndo({ ...t, key: ++toastKey.current, alwaysState: 'idle' });
  }

  /** After rules change: everything loaded, the header and the rule list. A pending
   *  "change the others too" offer is out of date (a new rule already did that). */
  async function afterRuleChange() {
    setOffer(null);
    summary.reload();
    ruleList.reload();
    await pages.reloadLoaded();
  }

  async function setCategory(t0: Transaction, id: string | null, viaKey: boolean, focusNext = false) {
    const t = findTxn(t0.id) ?? t0;
    if (id !== null && id === t.category && isHandSet(t.category_source)) return; // already set by hand
    const snapshot: CategoryState[] = [{ id: t.id, category: t.category, category_source: t.category_source }];
    if (viaKey) selectNextNeeding(t.id, focusNext);
    if (id) pushRecent(id);
    try {
      const u = await api.updateTransaction(t.id, { category: id });
      pages.replace([u]);
      summary.reload();
      const title = txnTitle(t);
      const match = ruleMatchFor(t);
      showUndo({
        title: id === null ? 'Back to automatic' : u.category_name,
        body: `for ${title}`,
        snapshot,
        always: id !== null && u.matching_rule_id === null && match.text && match.text.length <= MAX_RULE_TEXT ? { ...match, category: id, label: title } : null,
      });
      if (id !== null && !viaKey && selectedRef.current === t.id) setOffer({ txnId: t.id, ...match, category: id, categoryName: u.category_name });
      else setOffer((o) => (o?.txnId === t.id ? null : o));
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t change the category', body: errorMessage(e) });
    }
  }

  async function bulkSet(id: string | null) {
    const ids = [...checkedRef.current];
    if (!ids.length) return;
    try {
      // The endpoint takes 1000 at a time: send more in order, as one change (one toast, one Undo).
      const r: BulkCategoryResult = { items: [], previous: [], skipped: [], skipped_card_payments: [] };
      for (const part of chunks(ids)) {
        let p: BulkCategoryResult;
        try {
          p = await api.bulkSetCategory(part, id);
        } catch (e) {
          if (!r.items.length) throw e;
          // Some went through: keep them (and their Undo), say the rest didn't.
          toast.push({ tone: 'error', title: 'Couldn’t change all of them', body: `${plural(r.items.length, 'transaction')} changed. ${errorMessage(e)}` });
          break;
        }
        r.items.push(...p.items);
        r.previous.push(...p.previous);
        r.skipped.push(...p.skipped);
        r.skipped_card_payments!.push(...(p.skipped_card_payments ?? []));
      }
      pages.replace(r.items);
      summary.reload();
      setChecked(new Set());
      if (id) pushRecent(id);
      // "Paying off debt" (loans only): card payments are left out, and also listed in `skipped`.
      const cards = new Set(r.skipped_card_payments);
      const splits = r.skipped.filter((x) => !cards.has(x)).length;
      const skipped = [
        splits ? `${plural(splits, 'split transaction')} ${splits === 1 ? 'was' : 'were'} left as ${splits === 1 ? 'it is' : 'they are'}.` : '',
        cards.size ? `${plural(cards.size, 'card payment')} ${cards.size === 1 ? 'was' : 'were'} left out: ${cards.size === 1 ? 'it’s' : 'they’re'} already part of your Budget money.` : '',
      ]
        .filter(Boolean)
        .join(' ');
      // The bulk bar is gone with the ticks: land on what replaced it.
      requestAnimationFrame(() => focusEl(document.querySelector<HTMLElement>('.txn-select-needing:not(:disabled)') ?? document.querySelector<HTMLElement>('.txn-list [data-name]')));
      if (!r.items.length) {
        toast.push({ tone: 'info', title: 'Nothing changed', body: r.skipped.length === ids.length ? (cards.size ? skipped : 'Split transactions keep their split categories.') : `They already had that category.${skipped ? ` ${skipped}` : ''}` });
        return;
      }
      const keys = new Set(r.items.map(merchantKey));
      const one = keys.size === 1 ? r.items[0]! : null;
      const match = one ? ruleMatchFor(one) : null;
      const canRule = !!(id && one && match && match.text && match.text.length <= MAX_RULE_TEXT && r.items.every((t) => t.matching_rule_id === null));
      showUndo({
        title: id === null ? 'Back to automatic' : (catById.get(id)?.name ?? r.items[0]!.category_name),
        body: `for ${plural(r.items.length, 'transaction')}${skipped ? `. ${skipped}` : ''}`,
        snapshot: r.previous,
        always: canRule && id && one && match ? { ...match, category: id, label: txnTitle(one) } : null,
      });
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t change the categories', body: errorMessage(e) });
    }
  }

  async function addAlwaysRule() {
    const u = undoRef.current;
    if (!u?.always || u.alwaysState !== 'idle') return;
    const { field, text, category: cat } = u.always;
    setUndo({ ...u, alwaysState: 'busy' });
    // First in the list, so an older rule that also matches can't win over this choice.
    const req = api.rules.create({ field, op: 'is', text, action: 'category', category: cat, enabled: true, first: true });
    const save: AlwaysSave = { key: u.key, ruleId: req.then((r) => r.id, () => null), undone: false };
    alwaysSave.current = save;
    try {
      const rule = await req;
      if (save.undone) return; // Undo was pressed meanwhile: it deletes the rule and reloads
      setUndo((cur) =>
        cur && cur.key === u.key
          ? { ...cur, alwaysState: 'done', createdRuleId: rule.id, body: `${cur.body}${rule.matches > 0 ? ` · ${rule.matches.toLocaleString()} more updated by the new rule` : ''}` }
          : cur,
      );
      await afterRuleChange();
    } catch (e) {
      if (save.undone) return;
      setUndo((cur) => (cur && cur.key === u.key ? { ...cur, alwaysState: 'idle' } : cur));
      toast.push({ tone: 'error', title: 'Couldn’t add the rule', body: errorMessage(e) });
    } finally {
      if (alwaysSave.current === save) alwaysSave.current = null;
    }
  }

  async function runUndo() {
    const u = undoRef.current;
    if (!u) return;
    // The toast is about to go: keep keyboard focus on the list.
    if (document.activeElement?.closest('.txn-toast')) {
      const id = selectedRef.current;
      requestAnimationFrame(() => focusEl((id !== null && rowName(id)) || document.querySelector<HTMLElement>('.txn-list [data-name]')));
    }
    // "Always for…" still saving: wait for the rule so it can be deleted too.
    const save = alwaysSave.current?.key === u.key ? alwaysSave.current : null;
    if (save) save.undone = true;
    setUndo(null);
    setOffer(null);
    try {
      let rulesChanged = false;
      const createdRuleId = u.createdRuleId ?? (save ? await save.ruleId : null);
      if (createdRuleId != null) {
        rulesChanged = true;
        try {
          await api.rules.remove(createdRuleId);
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 404)) throw e;
        }
      }
      if (u.removedRule) {
        rulesChanged = true;
        const { rule, index } = u.removedRule;
        const again = await api.rules.create({
          field: rule.field,
          op: rule.op,
          text: rule.text,
          amount_op: rule.amount_op,
          amount: rule.amount,
          action: rule.action,
          category: rule.category,
          enabled: rule.enabled,
        });
        const order = (await api.rules.list()).sort((a, b) => a.position - b.position).map((r) => r.id).filter((id) => id !== again.id);
        order.splice(Math.min(index, order.length), 0, again.id);
        await api.rules.reorder(order);
      }
      for (const part of chunks(u.snapshot)) {
        const r = await api.restoreCategories(part);
        pages.replace(r.items);
      }
      if (rulesChanged) await afterRuleChange();
      else summary.reload();
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(e) });
    }
  }

  async function removeRule(rule: Rule) {
    const index = [...rules].sort((a, b) => a.position - b.position).findIndex((r) => r.id === rule.id);
    const text = ruleText(rule, catById);
    try {
      await api.rules.remove(rule.id);
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t remove the rule', body: errorMessage(e) });
      return;
    }
    showUndo({ title: 'Rule removed', body: text.replace(/^Rule: /, ''), snapshot: [], removedRule: { rule, index: Math.max(0, index) }, always: null });
    await afterRuleChange();
  }

  async function makeRule(t: Transaction) {
    const match = ruleMatchFor(t);
    if (!t.category || !match.text || match.text.length > MAX_RULE_TEXT) return;
    try {
      const rule = await api.rules.create({ ...match, op: 'is', action: 'category', category: t.category, enabled: true });
      // The rule also matches this row unless it was set by hand.
      const others = rule.matches - (isHandSet(t.category_source) ? 0 : 1);
      showUndo({
        title: 'Rule added',
        body: `${match.text} → ${t.category_name}${others > 0 ? ` · ${others.toLocaleString()} more updated` : ''}`,
        snapshot: [],
        createdRuleId: rule.id,
        always: null,
      });
      await afterRuleChange();
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t add the rule', body: errorMessage(e) });
    }
  }

  async function toggleTransfer(t: Transaction, v: boolean) {
    try {
      const u = await api.updateTransaction(t.id, { is_transfer: v });
      pages.replace([u]);
      summary.reload();
      // Day totals leave transfers out. (In "Needs a category" the row would drop out, so it stays.)
      if (view !== 'needs_category') void pages.reloadLoaded();
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t update the transaction', body: errorMessage(e) });
    }
  }

  async function selectAllNeeding() {
    setSelecting(true);
    const key = filterKey;
    try {
      const r = await api.transactionIds({ ...filters, needs_category: true });
      if (filterKeyRef.current !== key) return; // the filters changed meanwhile: these ids are for the old ones
      setChecked((prev) => new Set([...prev, ...r.ids]));
      if (r.truncated) toast.push({ tone: 'info', title: `Selected the first ${r.ids.length.toLocaleString()}`, body: `${r.total.toLocaleString()} need a category. Set these, then select the rest.` });
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t select them', body: errorMessage(e) });
    } finally {
      setSelecting(false);
    }
  }

  async function exportCsv() {
    setExporting(true);
    try {
      const blob = await api.exportTransactionsCsv(filters);
      saveBlob(blob, `iron-owl-transactions-${toISODate(new Date())}.csv`);
      const n = summary.data?.total ?? pages.total;
      toast.push({ tone: 'success', title: 'CSV exported', body: `${plural(n, 'transaction')}${unfiltered ? '' : ' matching your filters'}.`, timeout: 3500 });
    } catch (e) {
      toast.push({ tone: 'error', title: 'Export failed', body: errorMessage(e) });
    } finally {
      setExporting(false);
    }
  }

  // ---------------------------------------------------------------- picker, split, selection

  function openPicker(id: number, el: HTMLElement | null, viaKey: boolean) {
    const t = findTxn(id);
    if (!t || !categories.length) return;
    if (t.splits.length) return openSplit(t, el);
    const anchorEl = el ?? rowChip(id);
    const anchor = anchorEl?.getBoundingClientRect() ?? new DOMRect(window.innerWidth / 2 - 150, 120, 0, 0);
    setSelectedId(id);
    setPicker({ target: id, anchor, viaKey, returnFocus: el ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null) });
  }

  function openBulkPicker(el: HTMLElement) {
    if (!categories.length) return;
    setPicker({ target: 'bulk', anchor: el.getBoundingClientRect(), viaKey: false, returnFocus: el });
  }

  function pick(id: string | null) {
    const p = picker;
    if (!p) return;
    setPicker(null);
    if (p.target === 'bulk') {
      focusEl(p.returnFocus);
      void bulkSet(id);
      return;
    }
    const t = findTxn(p.target);
    if (!t) return;
    const fromList = !!(p.returnFocus && listRef.current?.contains(p.returnFocus));
    if (p.viaKey) {
      const hasNext = !!nextNeeding(t.id) || (!!pages.nextCursor && needingUnloaded());
      void setCategory(t, id, true, fromList);
      if (!hasNext) focusEl(p.returnFocus);
    } else {
      void setCategory(t, id, false);
      focusEl(p.returnFocus);
    }
  }

  function closePicker(reason: PopoverCloseReason) {
    const p = picker;
    setPicker(null);
    if (reason !== 'outside') focusEl(p?.returnFocus);
  }

  function openSplit(t: Transaction, el: HTMLElement | null) {
    if (!categories.length) return;
    splitReturn.current = el ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    setSelectedId(t.id);
    setSplitFor(t);
  }
  function closeSplit() {
    setSplitFor(null);
    const el = splitReturn.current;
    requestAnimationFrame(() => focusEl(el));
  }

  function toggleCheck(id: number) {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function closePanel(focusRow = true) {
    const id = selectedRef.current;
    setSelectedId(null);
    if (focusRow && id !== null) requestAnimationFrame(() => focusEl(rowName(id)));
  }

  // Rows get one stable handlers object; it calls the latest closures through a ref.
  const actions = useRef({ openPicker, setCategory, openSplit, toggleCheck });
  actions.current = { openPicker, setCategory, openSplit, toggleCheck };
  const handlers = useMemo<RowHandlers>(
    () => ({
      onSelect: (id, fromName) => {
        const open = selectedRef.current !== id;
        setSelectedId(open ? id : null);
        // Opened from the name button: take keyboard focus into the panel (or drawer). × / Esc bring it back.
        if (open && fromName) requestAnimationFrame(() => focusEl(sideRef.current?.querySelector<HTMLElement>('[data-panel-title]')));
      },
      onToggleCheck: (id) => actions.current.toggleCheck(id),
      onOpenPicker: (id, el) => actions.current.openPicker(id, el, false),
      onSuggest: (id, cat) => {
        const t = itemsRef.current.find((x) => x.id === id);
        if (t) void actions.current.setCategory(t, cat, false);
      },
      onOpenSplit: (id, el) => {
        const t = itemsRef.current.find((x) => x.id === id);
        if (t) actions.current.openSplit(t, el);
      },
    }),
    [],
  );

  // ---------------------------------------------------------------- keyboard

  function onKey(key: TxnKey): boolean {
    const items = itemsRef.current;
    const sel = selectedRef.current;
    const i = sel === null ? -1 : items.findIndex((t) => t.id === sel);
    const t = i >= 0 ? items[i]! : null;
    switch (key) {
      case 'down':
        if (!items.length) return false;
        if (!t) selectRow(items[0]!.id);
        else if (i < items.length - 1) selectRow(items[i + 1]!.id);
        else if (pages.nextCursor) {
          pending.current = { after: t.id, mode: 'next', focus: !!listRef.current?.contains(document.activeElement), pages: 1 };
          pages.loadMore();
        }
        return true;
      case 'up':
        if (!items.length) return false;
        selectRow(items[t ? Math.max(0, i - 1) : 0]!.id);
        return true;
      case 'category':
        if (!t) return false;
        // Scroll first; open on the next frame, after the scroll event (which would close the picker).
        scrollRowIntoView(t.id);
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            if (t.splits.length) openSplit(t, rowChip(t.id));
            else openPicker(t.id, null, true);
          }),
        );
        return true;
      case 'suggest1':
      case 'suggest2': {
        if (!t || !t.needs_category || t.splits.length) return false;
        const sugs = t.suggested_categories.filter((id) => {
          const c = catById.get(id);
          return c && !c.hidden;
        });
        const id = sugs[key === 'suggest1' ? 0 : 1];
        if (!id) return false;
        void setCategory(t, id, true, !!listRef.current?.contains(document.activeElement));
        return true;
      }
      case 'tick':
        if (!t) return false;
        toggleCheck(t.id);
        return true;
      case 'undo':
        if (!undoRef.current) return false;
        void runUndo();
        return true;
      case 'escape': {
        if (sel === null && !checkedRef.current.size) return false;
        const inPanel = !!sideRef.current?.contains(document.activeElement);
        setChecked(new Set());
        closePanel(inPanel);
        return true;
      }
    }
  }
  useTxnKeyboard(onKey, picker !== null || splitFor !== null);

  // ---------------------------------------------------------------- render

  const sameCount = selected ? pages.items.filter((t) => merchantKey(t) === merchantKey(selected)).length : 0;
  const empty =
    view === 'needs_category' && !search && filterCount === 0 ? (
      <EmptyState
        kind="transactions"
        title="Nothing needs a category"
        actions={
          <button type="button" className="btn" onClick={() => update({ view: null })}>
            Show all transactions
          </button>
        }
      >
        Every transaction is in a budget category. Nice work.
      </EmptyState>
    ) : !unfiltered ? (
      <EmptyState
        kind="search"
        title="No matching transactions"
        actions={
          <button type="button" className="btn" onClick={clearAll}>
            Clear filters
          </button>
        }
      >
        Try a shorter search, a wider date range, another category or tag, or all accounts.
      </EmptyState>
    ) : (
      <EmptyState
        kind="transactions"
        title="No transactions yet"
        actions={
          <Link to={linkTo} className="btn btn-primary">
            <Icon name="link" />
            Link a bank
          </Link>
        }
      >
        Transactions come from linked bank and card accounts. Manual accounts track balances only.
      </EmptyState>
    );

  const pickerTxn = picker && picker.target !== 'bulk' ? (pages.items.find((t) => t.id === picker.target) ?? null) : null;

  return (
    <div className="txn-page" style={{ '--txn-list-top': `${listTop}px`, '--txn-sticky-top': `${stickyTop}px` } as CSSProperties}>
      <div ref={headRef} className="txn-top">
        <TxnHeader
          summary={summary.data}
          view={view}
          searchDraft={searchDraft}
          onSearch={setSearchDraft}
          onView={(v) => update({ view: v === 'all' ? null : v })}
          filtersOpen={filtersOpen}
          filterCount={filterCount}
          onToggleFilters={() => setFiltersOpen((o) => !o)}
          exporting={exporting}
          canExport={!!summary.data && summary.data.total > 0}
          onExport={() => void exportCsv()}
        />
        {filtersOpen && (
          <TxnFilterRow
            accounts={accounts.data ?? []}
            categories={categories}
            tags={tagList.data ?? []}
            tagsLoaded={tagList.data !== undefined}
            accountId={accountId}
            category={category}
            tag={tag}
            start={start}
            end={end}
            onChange={update}
            onClear={clearFilters}
          />
        )}
        <TxnToolbar
          checkedCount={checked.size}
          needsCount={summary.data ? summary.data.needs_category : null}
          selecting={selecting}
          bulkOpen={picker?.target === 'bulk'}
          onSelectNeeding={() => void selectAllNeeding()}
          onSetCategory={openBulkPicker}
          onClear={() => setChecked(new Set())}
        />
      </div>

      <div className="txn-body">
        <TxnList
          listRef={listRef}
          pageScroll={pageScroll}
          items={pages.items}
          days={pages.days}
          loaded={pages.loaded}
          loading={pages.loading}
          stale={pages.refreshing && pages.loaded}
          error={pages.error}
          nextCursor={pages.nextCursor}
          firstDate={summary.data?.first_date ?? null}
          selectedId={selectedId}
          checked={checked}
          pickerTarget={picker?.target ?? null}
          catById={catById}
          groupIndex={groupIndex}
          accountLabels={accountLabels}
          handlers={handlers}
          empty={empty}
          onLoadMore={pages.loadMore}
          onRetry={pages.retry}
        />
        <aside ref={sideRef} className={`txn-side${selected ? ' has-selection' : ''}`} aria-label={selected ? 'Transaction details' : 'Summary'}>
          {selected ? (
            <TxnDetailsPanel
              t={selected}
              accountLabel={accountLabels.get(selected.account_id) ?? selected.account_name}
              categories={categories}
              catById={catById}
              groupIndex={groupIndex}
              groupNames={groupNames}
              rule={selected.matching_rule_id === null ? null : (rules.find((r) => r.id === selected.matching_rule_id) ?? null)}
              tags={tagList.data ?? []}
              sameCount={sameCount}
              offer={offer}
              pickerOpen={picker?.target === selected.id}
              onClose={() => closePanel()}
              onOpenPicker={(el) => openPicker(selected.id, el, false)}
              onUpdated={(u) => pages.replace([u])}
              onTagsChanged={tagList.reload}
              onTagCreated={(tg) => tagList.setData((prev) => [...(prev ?? []), tg])}
              onSplit={(el) => openSplit(selected, el)}
              onToggleTransfer={(v) => toggleTransfer(selected, v)}
              onRemoveRule={removeRule}
              onMakeRule={() => makeRule(selected)}
              onSelectSame={() => {
                const key = merchantKey(selected);
                setChecked((prev) => new Set([...prev, ...pages.items.filter((t) => merchantKey(t) === key).map((t) => t.id)]));
              }}
              onOfferClose={() => setOffer(null)}
              onOfferApplied={() => {
                setOffer(null);
                summary.reload();
                void pages.reloadLoaded();
              }}
            />
          ) : (
            <TxnSummaryPanel summary={summary.data} unfiltered={unfiltered} />
          )}
        </aside>
      </div>

      <div className="txn-toast-region" role="status" aria-live="polite">
        {undo && <TxnUndoToast toast={undo} onUndo={() => void runUndo()} onAlways={() => void addAlwaysRule()} onDismiss={dismissUndo} />}
      </div>

      {picker && (
        <CategoryPopover
          anchor={picker.anchor}
          current={pickerTxn && !pickerTxn.needs_category ? pickerTxn.category : null}
          source={pickerTxn ? pickerTxn.category_source : null}
          categories={categories}
          groups={groups}
          recent={recent}
          bulkCount={picker.target === 'bulk' ? checked.size : undefined}
          onPick={pick}
          onClose={closePicker}
        />
      )}

      {splitFor && (
        <SplitEditor
          txn={splitFor}
          categories={categories}
          onClose={closeSplit}
          onSaved={(u) => {
            pages.replace([u]);
            summary.reload();
            setOffer((o) => (o?.txnId === u.id ? null : o));
          }}
        />
      )}
    </div>
  );
}
