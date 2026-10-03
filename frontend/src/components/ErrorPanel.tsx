import { errorMessage } from '../api';
import { EmptyState } from './EmptyState';
import { Icon } from './Icon';

export function ErrorPanel({ error, onRetry, title = 'Couldn’t load this page' }: { error: unknown; onRetry?: () => void; title?: string }) {
  return (
    <div className="panel" role="alert">
      <EmptyState
        kind="offline"
        title={title}
        actions={
          onRetry && (
            <button type="button" className="btn" onClick={onRetry}>
              <Icon name="sync" />
              Try again
            </button>
          )
        }
      >
        {errorMessage(error)}
      </EmptyState>
    </div>
  );
}
