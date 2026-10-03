import { useId } from 'react';
import { askNote, askTitle, ASK_QUESTION, noLabel, yesLabel, type PriceAsk } from './priceAsk';

/**
 * Release 3.19: "Netflix now charges $17.99. Update your amount?" with Yes / No (amber: it
 * needs the user; buttons at least 44px tall). Used in the List view's row, Item details and Find.
 */
export function PriceAskBox({ ask, busy, onAnswer }: { ask: PriceAsk; busy: boolean; onAnswer: (a: PriceAsk, yes: boolean) => void }) {
  const id = useId();
  return (
    <div className="price-ask" role="group" aria-labelledby={id}>
      <p className="price-ask-text" id={id}>
        <span className="price-ask-tag">New price</span> <strong>{askTitle(ask)}</strong> {ASK_QUESTION}
      </p>
      <p className="price-ask-note">{askNote(ask)}</p>
      <PriceAskButtons ask={ask} busy={busy} onAnswer={onAnswer} />
    </div>
  );
}

export function PriceAskButtons({ ask, busy, onAnswer }: { ask: PriceAsk; busy: boolean; onAnswer: (a: PriceAsk, yes: boolean) => void }) {
  return (
    <div className="price-ask-actions">
      <button type="button" className="btn price-ask-btn price-ask-yes" aria-label={yesLabel(ask)} disabled={busy} onClick={() => onAnswer(ask, true)}>
        Yes
      </button>
      <button type="button" className="btn price-ask-btn" aria-label={noLabel(ask)} disabled={busy} onClick={() => onAnswer(ask, false)}>
        No
      </button>
    </div>
  );
}
