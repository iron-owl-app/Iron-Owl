import { useId, useRef, useState, type CSSProperties, type DragEvent, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { api, type CategoryGroup, type CategoryPatch, type TxnCategory } from '../../api';
import { useApi } from '../../lib/useApi';
import { plural } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { useSettings } from './context';
import { DialogActions, StDialog, StSwitch, useFail, useSay } from './parts';

/** The design's 8 colors: oklch(0.74 0.13 H). */
const HUES = [20, 55, 85, 130, 160, 200, 250, 300];

type CatDraft = {
  mode: 'new' | 'edit';
  id?: string;
  name: string;
  group: number | null;
  hue: number;
  hidden: boolean;
  custom: boolean;
  spending: boolean;
};
type GroupDraft = { mode: 'new' | 'edit'; id?: number; name: string };
type Drag = { type: 'group'; i: number } | { type: 'cat'; id: string };
/** Where a drag would land: a group tile (`gid`, null = Other) for a category, a slot for a group. */
type Over = { gid: number | null } | { slot: number };

export function GripIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="9" cy="6" r="1.6" />
      <circle cx="15" cy="6" r="1.6" />
      <circle cx="9" cy="12" r="1.6" />
      <circle cx="15" cy="12" r="1.6" />
      <circle cx="9" cy="18" r="1.6" />
      <circle cx="15" cy="18" r="1.6" />
    </svg>
  );
}

const hueStyle = (h: number) => ({ '--h': h }) as CSSProperties;

/**
 * Settings › Categories (design D7 tab 1): spending categories as pills on group tiles (drag a
 * tile to reorder groups, a pill to move it to another group; the grip also takes ArrowUp /
 * ArrowDown), then Other (no group), a folded "Hidden from Budget" section and a folded
 * "Income, transfers and loan payments" section. Every move shows Undo; deleting a category
 * or group is undone from a server snapshot.
 */
export function CategoriesTab() {
  const { goTab } = useSettings();
  const say = useSay();
  const fail = useFail();
  const cats = useApi(() => api.categories.list(), []);
  const groups = useApi(() => api.categoryGroups.list(), []);
  // Categories that hold a goal (Goals page) can't be deleted or hidden here (the server refuses too).
  const goals = useApi(() => api.goals.get(), []);
  const [cd, setCd] = useState<CatDraft | null>(null);
  const [gd, setGd] = useState<GroupDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const drag = useRef<Drag | null>(null);
  const [dragI, setDragI] = useState<number | null>(null);
  const [dragCat, setDragCat] = useState<string | null>(null);
  const [over, setOver] = useState<Over | null>(null);
  const uid = useId();

  const all = cats.data ?? [];
  const gList = [...(groups.data ?? [])].sort((a, b) => a.position - b.position);
  // Like Budget: a group whose categories are all income, transfer or fixed ones is left off.
  const shown = gList.filter((g) => {
    const members = all.filter((c) => c.group_id === g.id);
    return members.length === 0 || members.some((c) => c.kind === 'spending');
  });
  const shownIds = new Set(shown.map((g) => g.id));
  const spend = all.filter((c) => c.kind === 'spending');
  const groupOf = (c: TxnCategory) => (c.group_id !== null && shownIds.has(c.group_id) ? c.group_id : null);
  const inGroup = (gid: number | null) => spend.filter((c) => !c.hidden && groupOf(c) === gid);
  const hidden = spend.filter((c) => c.hidden);
  const other = all.filter((c) => c.kind !== 'spending');
  const groupName = (gid: number | null) => (gid === null ? 'Other' : (gList.find((g) => g.id === gid)?.name ?? 'Other'));

  const goalOf = (id: string | undefined) => (id ? (goals.data?.goals.find((g) => g.category_id === id) ?? null) : null);
  const replace = (c: TxnCategory) => cats.setData((prev) => (prev ?? []).map((x) => (x.id === c.id ? c : x)));

  // ---------------------------------------------------------------- moves

  async function reorder(from: number, to: number) {
    if (to > from) to--;
    if (to === from || to < 0 || to >= shown.length) return;
    const next = [...shown];
    const [g] = next.splice(from, 1);
    next.splice(to, 0, g!);
    // Groups left off the board keep their places in the full order.
    const nextSet = new Set(next.map((x) => x.id));
    let k = 0;
    const full = gList.map((x) => (nextSet.has(x.id) ? next[k++]! : x));
    const prevIds = gList.map((x) => x.id);
    groups.setData(full.map((x, i) => ({ ...x, position: i })));
    try {
      groups.setData(await api.categoryGroups.reorder(full.map((x) => x.id)));
      say(`${g!.name} moved.`, { undo: async () => groups.setData(await api.categoryGroups.reorder(prevIds)) });
    } catch (e) {
      groups.reload();
      fail('Couldn’t move the group', e);
    }
  }

  async function moveCat(id: string, gid: number | null) {
    const c = all.find((x) => x.id === id);
    if (!c || (groupOf(c) === gid && !c.hidden)) return;
    const prev: CategoryPatch = { group_id: c.group_id, hidden: c.hidden };
    const patch: CategoryPatch = c.hidden ? { group_id: gid, hidden: false } : { group_id: gid };
    replace({ ...c, group_id: gid, hidden: false });
    try {
      replace(await api.categories.update(c.id, patch));
      say(`${c.name} moved to ${groupName(gid)}.`, { undo: async () => replace(await api.categories.update(c.id, prev)) });
    } catch (e) {
      replace(c);
      fail(`Couldn’t move ${c.name}`, e);
    }
  }

  function endDrag() {
    drag.current = null;
    setDragI(null);
    setDragCat(null);
    setOver(null);
  }

  function tileHandlers(i: number, gid: number | null) {
    const isOther = gid === null;
    return {
      draggable: !isOther,
      onDragStart: (e: DragEvent) => {
        if (isOther) return;
        drag.current = { type: 'group', i };
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', shown[i]?.name ?? '');
        setDragI(i);
      },
      onDragOver: (e: DragEvent<HTMLDivElement>) => {
        const d = drag.current;
        if (!d) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        let o: Over;
        if (d.type === 'cat') o = { gid };
        else {
          const r = e.currentTarget.getBoundingClientRect();
          o = { slot: isOther ? shown.length : e.clientY < r.top + r.height / 2 ? i : i + 1 };
        }
        setOver((cur) => (cur && JSON.stringify(cur) === JSON.stringify(o) ? cur : o));
      },
      onDrop: (e: DragEvent) => {
        e.preventDefault();
        const d = drag.current;
        const o = over;
        endDrag();
        if (!d || !o) return;
        if (d.type === 'cat') void moveCat(d.id, 'gid' in o ? o.gid : gid);
        else if ('slot' in o) void reorder(d.i, o.slot);
      },
      onDragEnd: endDrag,
    };
  }

  function onGripKey(e: KeyboardEvent<HTMLButtonElement>, i: number, g: CategoryGroup) {
    let to = -1;
    if (e.key === 'ArrowUp' && i > 0) to = i - 1;
    else if (e.key === 'ArrowDown' && i < shown.length - 1) to = i + 2;
    if (to < 0) return;
    e.preventDefault();
    void reorder(i, to).then(() => requestAnimationFrame(() => document.getElementById(`${uid}-grip-${g.id}`)?.focus()));
  }

  function pillDrag(c: TxnCategory) {
    return {
      draggable: true,
      onDragStart: (e: DragEvent) => {
        e.stopPropagation();
        drag.current = { type: 'cat', id: c.id };
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', c.name);
        setDragCat(c.id);
      },
      onDragEnd: endDrag,
    };
  }

  // ---------------------------------------------------------------- windows

  const openCat = (c: TxnCategory) =>
    setCd({ mode: 'edit', id: c.id, name: c.name, group: groupOf(c), hue: c.hue, hidden: c.hidden, custom: c.custom, spending: c.kind === 'spending' });
  const newCat = (gid: number | null) =>
    setCd({ mode: 'new', name: '', group: gid, hue: HUES[spend.length % HUES.length]!, hidden: false, custom: true, spending: true });

  const cdName = cd?.name.trim() ?? '';
  const cdDup = !!cd && !!cdName && all.some((c) => c.id !== cd.id && c.name.toLowerCase() === cdName.toLowerCase());
  const cdOk = !!cdName && !cdDup;
  const cdGoal = cd?.mode === 'edit' ? goalOf(cd.id) : null;

  async function saveCat() {
    if (!cd || !cdOk) return;
    setBusy(true);
    try {
      if (cd.mode === 'new') {
        const c = await api.categories.create({ name: cdName, kind: 'spending', hue: cd.hue, group_id: cd.group });
        cats.setData((prev) => [...(prev ?? []), c]);
        say(`${c.name} was added.`);
      } else {
        const c = all.find((x) => x.id === cd.id);
        if (!c) return setCd(null);
        const patch: CategoryPatch = {};
        if (cdName !== c.name) patch.name = cdName;
        if (cd.hue !== c.hue) patch.hue = cd.hue;
        if (cd.hidden !== c.hidden) patch.hidden = cd.hidden;
        if (cd.spending && cd.group !== groupOf(c)) patch.group_id = cd.group;
        if (Object.keys(patch).length) replace(await api.categories.update(c.id, patch));
        say(`${cdName} was saved.`);
      }
      setCd(null);
    } catch (e) {
      fail('Couldn’t save the category', e);
    } finally {
      setBusy(false);
    }
  }

  async function deleteCat() {
    const c = cd && all.find((x) => x.id === cd.id);
    if (!c) return;
    setBusy(true);
    try {
      const snap = await api.categories.snapshot(c.id);
      await api.categories.remove(c.id);
      cats.setData((prev) => (prev ?? []).filter((x) => x.id !== c.id));
      setCd(null);
      const n = snap.rules.length;
      const rulesNote = n === 0 ? '' : n === 1 ? ' The rule that used it was deleted too.' : ` The ${n} rules that used it were deleted too.`;
      say(`${c.name} was deleted. Its purchases go back to automatic sorting.${rulesNote}`, {
        undo: async () => {
          try {
            await api.categories.restore(snap);
          } finally {
            cats.reload();
            groups.reload();
          }
        },
      });
    } catch (e) {
      fail(`Couldn’t delete ${c.name}`, e);
    } finally {
      setBusy(false);
    }
  }

  const gdName = gd?.name.trim().replace(/\s+/g, ' ') ?? '';
  const gdDup = !!gd && !!gdName && gList.some((g) => g.id !== gd.id && g.name.toLowerCase() === gdName.toLowerCase());
  const gdOk = !!gdName && !gdDup;
  const gdCount = gd?.id !== undefined ? all.filter((c) => c.group_id === gd.id).length : 0;

  async function saveGroup() {
    if (!gd || !gdOk) return;
    setBusy(true);
    try {
      if (gd.mode === 'new') {
        const g = await api.categoryGroups.create(gdName);
        groups.setData((prev) => [...(prev ?? []), g]);
        say(`${g.name} group was added. Add categories to it below.`);
      } else if (gd.id !== undefined) {
        const g = gList.find((x) => x.id === gd.id);
        if (g && g.name !== gdName) {
          const u = await api.categoryGroups.rename(gd.id, gdName);
          groups.setData((prev) => (prev ?? []).map((x) => (x.id === u.id ? { ...x, name: u.name } : x)));
        }
        say('Group saved.');
      }
      setGd(null);
    } catch (e) {
      fail('Couldn’t save the group', e);
    } finally {
      setBusy(false);
    }
  }

  async function deleteGroup() {
    const g = gd?.id !== undefined ? gList.find((x) => x.id === gd.id) : undefined;
    if (!g) return;
    setBusy(true);
    const snap = { id: g.id, name: g.name, position: gList.indexOf(g), category_ids: all.filter((c) => c.group_id === g.id).map((c) => c.id) };
    try {
      await api.categoryGroups.remove(g.id);
      groups.setData((prev) => (prev ?? []).filter((x) => x.id !== g.id));
      cats.setData((prev) => (prev ?? []).map((c) => (c.group_id === g.id ? { ...c, group_id: null } : c)));
      setGd(null);
      say(`${g.name} group was deleted.`, {
        undo: async () => {
          try {
            groups.setData(await api.categoryGroups.restore(snap));
          } finally {
            cats.reload();
          }
        },
      });
    } catch (e) {
      fail('Couldn’t delete the group', e);
    } finally {
      setBusy(false);
    }
  }

  async function starter() {
    setStarting(true);
    try {
      groups.setData(await api.categoryGroups.starter());
      cats.reload();
      say('Suggested groups were added. Drag categories to change them.');
    } catch (e) {
      fail('Couldn’t add the suggested groups', e);
    } finally {
      setStarting(false);
    }
  }

  // ---------------------------------------------------------------- render

  const loading = (cats.loading && !cats.data) || (groups.loading && !groups.data);
  const error = (cats.error && !cats.data) || (groups.error && !groups.data);
  const count = (n: number) => plural(n, 'category', 'categories');
  const slotAt = (i: number) => {
    const d = drag.current;
    return !!d && d.type === 'group' && !!over && 'slot' in over && over.slot === i && i !== d.i && i !== d.i + 1;
  };
  const catTarget = (gid: number | null) => !!over && 'gid' in over && over.gid === gid && drag.current?.type === 'cat';

  const pill = (c: TxnCategory, dim = false, draggable = true) => (
    <button
      key={c.id}
      type="button"
      className={`st-pill${dim ? ' is-dim' : ''}${dragCat === c.id ? ' is-moving' : ''}`}
      title={draggable ? 'Click to edit, or drag to another group' : 'Click to edit'}
      onClick={() => openCat(c)}
      {...(draggable ? pillDrag(c) : {})}
    >
      <span className="st-cdot" style={hueStyle(c.hue)} aria-hidden="true" />
      {c.name}
    </button>
  );

  const otherCats = inGroup(null);

  return (
    <section className="st-card" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Categories</h2>
          <p className="st-desc">Every purchase gets a category, like Groceries or Gas. Groups put categories together into sections on the Budget page.</p>
          <p className="st-desc-2">Drag a group to change its order. Drag a category onto another group to move it.</p>
        </div>
        <div className="st-card-actions">
          <button type="button" className="st-btn st-btn-primary" onClick={() => newCat(null)} disabled={loading}>
            + Add a category
          </button>
          <button type="button" className="st-btn st-btn-outline" onClick={() => setGd({ mode: 'new', name: '' })} disabled={loading}>
            + Add a group
          </button>
        </div>
      </div>

      {loading ? (
        <div className="st-board" aria-busy="true" aria-label="Loading categories">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} height={120} style={{ borderRadius: 12 }} />
          ))}
        </div>
      ) : error ? (
        <p className="st-error">
          Iron Owl couldn’t load your categories.{' '}
          <button
            type="button"
            className="st-link st-link-inline"
            onClick={() => {
              cats.reload();
              groups.reload();
            }}
          >
            Try again
          </button>
        </p>
      ) : (
        <div className="st-board">
          {shown.map((g, i) => {
            const list = inGroup(g.id);
            return (
              <div
                key={g.id}
                className={`st-group${dragI === i ? ' is-dragging' : ''}${catTarget(g.id) ? ' is-target' : ''}${slotAt(i) ? ' is-slot' : ''}`}
                role="group"
                aria-label={g.name}
                {...tileHandlers(i, g.id)}
              >
                <div className="st-group-head">
                  <button
                    type="button"
                    id={`${uid}-grip-${g.id}`}
                    className="st-grip"
                    aria-label={`Move ${g.name}. Drag, or use the up and down arrow keys.`}
                    title="Drag to move"
                    onKeyDown={(e) => onGripKey(e, i, g)}
                  >
                    <GripIcon />
                  </button>
                  <span className="st-group-title">
                    <span className="st-group-name">{g.name}</span>
                    <span className="st-group-count">{count(list.length)}</span>
                  </span>
                  <button type="button" className="st-btn st-btn-plain st-btn-sm" onClick={() => setGd({ mode: 'edit', id: g.id, name: g.name })}>
                    Edit group
                  </button>
                </div>
                <div className="st-pills">
                  {list.map((c) => pill(c))}
                  <button type="button" className="st-pill is-add" onClick={() => newCat(g.id)} aria-label={`Add a category to ${g.name}`}>
                    + Add
                  </button>
                </div>
              </div>
            );
          })}

          <div
            className={`st-group${catTarget(null) ? ' is-target' : ''}${slotAt(shown.length) ? ' is-slot' : ''}`}
            role="group"
            aria-label="Other"
            {...tileHandlers(shown.length, null)}
          >
            <div className="st-group-head">
              <span className="st-group-title">
                <span className="st-group-name">Other</span>
                <span className="st-group-count">{count(otherCats.length)}</span>
              </span>
            </div>
            {otherCats.length > 0 && <p className="st-group-hint">These don’t have a group yet. Drag one onto a group, or click it to pick one.</p>}
            {gList.length === 0 && (
              <p className="st-group-hint">
                No groups yet.{' '}
                <button type="button" className="st-link st-link-inline" onClick={() => void starter()} disabled={starting}>
                  {starting ? 'Adding…' : 'Start with suggested groups'}
                </button>{' '}
                (Home, Car, Food and a few more), or add your own.
              </p>
            )}
            <div className="st-pills">
              {otherCats.map((c) => pill(c))}
              <button type="button" className="st-pill is-add" onClick={() => newCat(null)} aria-label="Add a category without a group">
                + Add
              </button>
            </div>
          </div>

          {hidden.length > 0 && (
            <details className="st-fold">
              <summary>Hidden from Budget ({hidden.length})</summary>
              <p className="st-fold-help">These don’t show on the Budget page. Click one to show it again.</p>
              <div className="st-pills">{hidden.map((c) => pill(c, true, false))}</div>
            </details>
          )}

          {other.length > 0 && (
            <details className="st-fold">
              <summary>Income, transfers and loan payments ({other.length})</summary>
              <p className="st-fold-help">These aren’t spending, so they don’t go in groups. Click one to rename it, change its color or hide it.</p>
              <div className="st-pills">{other.map((c) => pill(c, c.hidden, false))}</div>
            </details>
          )}

          <p className="st-footnote">
            Money coming in, like paychecks, uses the Income category. Set it up in the{' '}
            <button type="button" className="st-link st-link-inline" onClick={() => goTab('pay')}>
              Paychecks tab
            </button>
            . To sort purchases automatically, use the{' '}
            <button type="button" className="st-link st-link-inline" onClick={() => goTab('rules')}>
              Rules tab
            </button>
            .
          </p>
        </div>
      )}

      <StDialog open={cd !== null} title={cd?.mode === 'edit' ? 'Edit category' : 'New category'} onClose={() => setCd(null)} onSubmit={() => void saveCat()} busy={busy}>
        {cd && (
          <>
            <div className="st-field">
              <label className="st-field-label" htmlFor={`${uid}-cname`}>
                Name
              </label>
              <input
                id={`${uid}-cname`}
                className="st-input"
                value={cd.name}
                maxLength={60}
                autoComplete="off"
                placeholder="For example, Coffee"
                onChange={(e) => setCd({ ...cd, name: e.target.value })}
                aria-invalid={cdDup || undefined}
                aria-describedby={cdDup ? `${uid}-cdup` : undefined}
              />
            </div>
            {cdDup && (
              <p className="st-alert" id={`${uid}-cdup`} role="alert">
                You already have a category with that name.
              </p>
            )}
            {cd.spending && (
              <div className="st-field">
                <label className="st-field-label" htmlFor={`${uid}-cgroup`}>
                  Group
                </label>
                <select
                  id={`${uid}-cgroup`}
                  className="st-input"
                  value={cd.group === null ? '' : String(cd.group)}
                  onChange={(e) => setCd({ ...cd, group: e.target.value ? Number(e.target.value) : null })}
                >
                  {shown.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                  <option value="">Other (no group)</option>
                </select>
              </div>
            )}
            <div className="st-field">
              <span className="st-field-label" id={`${uid}-ccolor`}>
                Color
              </span>
              <div role="radiogroup" aria-labelledby={`${uid}-ccolor`} className="st-swatches">
                {HUES.map((h, i) => (
                  <button
                    key={h}
                    type="button"
                    role="radio"
                    className="st-swatch"
                    aria-checked={cd.hue === h}
                    aria-label={`Color ${i + 1} of ${HUES.length}`}
                    tabIndex={cd.hue === h || (!HUES.includes(cd.hue) && i === 0) ? 0 : -1}
                    style={hueStyle(h)}
                    onClick={() => setCd({ ...cd, hue: h })}
                    onKeyDown={(e) => {
                      const j = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? (i + 1) % HUES.length : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? (i + HUES.length - 1) % HUES.length : -1;
                      if (j < 0) return;
                      e.preventDefault();
                      setCd({ ...cd, hue: HUES[j]! });
                      (e.currentTarget.parentElement?.children[j] as HTMLElement | undefined)?.focus();
                    }}
                  >
                    <span />
                  </button>
                ))}
              </div>
            </div>
            {cdGoal ? (
              <p className="st-help-faint">
                Holds a goal ·{' '}
                <Link to={`/goals?edit=${cdGoal.id}`} className="st-link st-link-inline">
                  Edit on the Goals page <span aria-hidden="true">→</span>
                </Link>
              </p>
            ) : (
              <>
                {cd.mode === 'edit' && (
                  <div className="st-dialog-row">
                    <span className="st-row-text">
                      <span className="st-label">Show on the Budget page</span>
                      <span className="st-help">Hide ones you never use.</span>
                    </span>
                    <StSwitch checked={!cd.hidden} onChange={(on) => setCd({ ...cd, hidden: !on })} label="Show on the Budget page" onText="Shown" offText="Hidden" />
                  </div>
                )}
                {cd.mode === 'edit' && !cd.custom && <p className="st-help-faint">This one came with Iron Owl. You can hide it, but not delete it.</p>}
              </>
            )}
            <DialogActions
              left={
                cd.mode === 'edit' && cd.custom && !cdGoal ? (
                  <button type="button" className="st-btn st-btn-danger st-btn-lg" onClick={() => void deleteCat()} disabled={busy}>
                    Delete
                  </button>
                ) : undefined
              }
              onCancel={() => setCd(null)}
              submitLabel={cd.mode === 'edit' ? 'Save' : 'Add category'}
              submitDisabled={!cdOk}
              busy={busy}
            />
          </>
        )}
      </StDialog>

      <StDialog open={gd !== null} size={480} title={gd?.mode === 'edit' ? 'Edit group' : 'New group'} onClose={() => setGd(null)} onSubmit={() => void saveGroup()} busy={busy}>
        {gd && (
          <>
            <div className="st-field">
              <label className="st-field-label" htmlFor={`${uid}-gname`}>
                Group name
              </label>
              <input
                id={`${uid}-gname`}
                className="st-input"
                value={gd.name}
                maxLength={40}
                autoComplete="off"
                placeholder="For example, Kids"
                onChange={(e) => setGd({ ...gd, name: e.target.value })}
                aria-invalid={gdDup || undefined}
                aria-describedby={gdDup ? `${uid}-gdup` : undefined}
              />
            </div>
            {gdDup && (
              <p className="st-alert" id={`${uid}-gdup`} role="alert">
                You already have a group with that name.
              </p>
            )}
            {gd.mode === 'edit' && (
              <p className="st-help">
                {gdCount === 0
                  ? 'This group has no categories.'
                  : `If you delete this group, its ${gdCount === 1 ? 'category moves' : `${gdCount} categories move`} to Other. No purchases change.`}
              </p>
            )}
            <DialogActions
              left={
                gd.mode === 'edit' ? (
                  <button type="button" className="st-btn st-btn-danger st-btn-lg" onClick={() => void deleteGroup()} disabled={busy}>
                    Delete group
                  </button>
                ) : undefined
              }
              onCancel={() => setGd(null)}
              submitLabel={gd.mode === 'edit' ? 'Save' : 'Add group'}
              submitDisabled={!gdOk}
              busy={busy}
            />
          </>
        )}
      </StDialog>
    </section>
  );
}

