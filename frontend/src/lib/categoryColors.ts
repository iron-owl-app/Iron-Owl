import type { CSSProperties } from 'react';
import type { CategoryGroup, TxnCategory } from '../api';

/**
 * Category colors shared by Reports and Transactions. Groups have no stored color, so a
 * group's hue comes from its position (the order of GET /api/category-groups): the n-th
 * group gets GROUP_HUES[n % 8]. Never derive a hue from a group or category name.
 */
export const GROUP_HUES = [266, 25, 150, 200, 300, 85, 340, 230];
export const UNGROUPED_HUE = 262;

/** Transfers get almost no chroma so they read as neutral. */
const TRANSFER_K = 0.015;

/** `hue` for oklch, `k` scales every chroma (1 = full color). */
export interface CategoryTone {
  hue: number;
  k: number;
}

/** group id → index in position order (ties by id), for `categoryTone`. */
export function groupIndexMap(groups: readonly Pick<CategoryGroup, 'id' | 'position'>[]): Map<number, number> {
  const sorted = [...groups].sort((a, b) => a.position - b.position || a.id - b.id);
  return new Map(sorted.map((g, i) => [g.id, i]));
}

export function groupHueAt(index: number): number {
  return GROUP_HUES[index % GROUP_HUES.length]!;
}

/**
 * A category's tone: its group's hue when it's in a group, else its own hue.
 * `groupIndex` is the category's group position (from `groupIndexMap`), or null/undefined.
 */
export function categoryTone(cat: Pick<TxnCategory, 'hue' | 'kind'>, groupIndex: number | null | undefined): CategoryTone {
  return {
    hue: groupIndex === null || groupIndex === undefined ? cat.hue : groupHueAt(groupIndex),
    k: cat.kind === 'transfer' ? TRANSFER_K : 1,
  };
}

/** Tone lookup for a category id, with a fallback hue for ids the list doesn't know. */
export function toneFor(
  id: string | null,
  byId: ReadonlyMap<string, TxnCategory>,
  groupIndex: ReadonlyMap<number, number>,
  fallbackHue = UNGROUPED_HUE,
): CategoryTone {
  const c = byId.get(id ?? 'OTHER');
  if (!c) return { hue: fallbackHue, k: 1 };
  return categoryTone(c, c.group_id === null ? null : groupIndex.get(c.group_id));
}

/** Inline custom properties for `.txn-chip` and friends (`--h`, `--k`). */
export function toneStyle(t: CategoryTone): CSSProperties {
  return { '--h': t.hue, '--k': t.k } as CSSProperties;
}
