import { Fragment } from 'react';
import { Icon } from '../../components/Icon';

const KEYS: [string[], string][] = [
  [['↑', '↓'], 'Move'],
  [['C'], 'Category'],
  [['1', '2'], 'Accept suggestion'],
  [['X'], 'Select'],
  [['U'], 'Undo'],
  [['Esc'], 'Close'],
];

/** Keyboard hints (hidden on narrow screens, where there's no keyboard to speak of). */
export function TxnKeyHints() {
  return (
    <p className="txn-keys" aria-label="Keyboard shortcuts">
      {KEYS.map(([keys, label]) => (
        <span key={label} className="txn-key">
          {keys.map((k, i) => (
            <Fragment key={k}>
              {i > 0 && ' '}
              <kbd>{k}</kbd>
            </Fragment>
          ))}
          {label}
        </span>
      ))}
    </p>
  );
}

/** Bulk bar when rows are ticked, else "Select all N needing a category"; key hints on the right. */
export function TxnToolbar({
  checkedCount,
  needsCount,
  selecting,
  bulkOpen,
  onSelectNeeding,
  onSetCategory,
  onClear,
}: {
  checkedCount: number;
  needsCount: number | null;
  selecting: boolean;
  bulkOpen: boolean;
  onSelectNeeding: () => void;
  onSetCategory: (anchor: HTMLElement) => void;
  onClear: () => void;
}) {
  return (
    <div className="txn-toolbar">
      {checkedCount > 0 ? (
        <div className="txn-bulk" role="group" aria-label="Selected transactions">
          <span className="txn-bulk-count" aria-live="polite">
            {checkedCount.toLocaleString()} selected
          </span>
          <button type="button" className="txn-bulk-set" onClick={(e) => onSetCategory(e.currentTarget)} aria-haspopup="dialog" aria-expanded={bulkOpen}>
            Set category
            <Icon name="chevronDown" />
          </button>
          <button type="button" className="txn-bulk-clear" onClick={onClear}>
            Clear
          </button>
        </div>
      ) : (
        <button type="button" className="btn btn-sm txn-select-needing" onClick={onSelectNeeding} disabled={!needsCount || selecting}>
          {needsCount ? `Select all ${needsCount.toLocaleString()} needing a category` : 'Nothing needs a category'}
        </button>
      )}
      <TxnKeyHints />
    </div>
  );
}
