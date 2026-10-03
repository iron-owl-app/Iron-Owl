import { Icon } from '../../components/Icon';

/** One card couldn't load (its section is in GET /api/dashboard `errors`): say so and offer Try again. */
export function CardError({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <div className="home-card-error" role="alert">
      <p>Iron Owl couldn’t load {what} just now.</p>
      <button type="button" className="home-btn home-btn-quiet" onClick={onRetry}>
        <Icon name="sync" />
        Try again
      </button>
    </div>
  );
}
