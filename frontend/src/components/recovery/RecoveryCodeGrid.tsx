import { formatGroup, spokenDigits } from '../../lib/recoveryCode';

/**
 * The 6 groups of a new recovery sheet, numbered, in large monospace digits ("739 151").
 * Screen readers hear "Group 1: 7 3 9 1 5 1". Text selection is off: the sheet is meant to go
 * on paper, and there is deliberately no Copy button.
 */
export function RecoveryCodeGrid({ groups, compact }: { groups: readonly string[]; compact?: boolean }) {
  return (
    <ol className={`rs-grid${compact ? ' rs-grid-compact' : ''}`} aria-label="Your recovery sheet numbers">
      {groups.map((g, i) => (
        <li key={i} className="rs-group">
          <span className="rs-n" aria-hidden="true">
            {i + 1}
          </span>
          <span className="rs-digits" aria-hidden="true">
            {formatGroup(g)}
          </span>
          <span className="sr-only">
            Group {i + 1}: {spokenDigits(g)}
          </span>
        </li>
      ))}
    </ol>
  );
}
