import { useCallback, useEffect, useRef } from 'react';
import { usePlaidLink, type PlaidLinkError, type PlaidLinkOnExitMetadata } from 'react-plaid-link';

type LauncherProps = {
  token: string;
  onSuccess: (publicToken: string) => void;
  onExit: (error: PlaidLinkError | null, metadata: PlaidLinkOnExitMetadata | null) => void;
};

/**
 * Mounted only while a Link session is in progress (keyed by token). Opens
 * Plaid Link as soon as the SDK is ready and reports back exactly once.
 * Callbacks are read through refs so the Plaid handler never sees stale props.
 *
 * Plaid Link renders its own full-screen iframe on <body>, so callers must not
 * keep a modal <dialog> open while this is mounted (a modal dialog makes the
 * rest of the page, including Plaid's iframe, inert).
 */
export function PlaidLinkLauncher(props: LauncherProps) {
  // Dev mock only (`npm run dev:mock`): statically false in production builds, so
  // the fake launcher below is dropped from the bundle.
  if (import.meta.env.DEV && import.meta.env.MODE === 'mock' && props.token.startsWith('link-sandbox-mock')) {
    return <MockPlaidLink {...props} />;
  }
  return <RealPlaidLink {...props} />;
}

function RealPlaidLink({ token, onSuccess, onExit }: LauncherProps) {
  const opened = useRef(false);
  const done = useRef(false);
  const successRef = useRef(onSuccess);
  const exitRef = useRef(onExit);
  successRef.current = onSuccess;
  exitRef.current = onExit;

  const handleSuccess = useCallback((publicToken: string | null) => {
    if (done.current) return;
    done.current = true;
    successRef.current(publicToken ?? '');
  }, []);

  const handleExit = useCallback((error: PlaidLinkError | null, metadata: PlaidLinkOnExitMetadata) => {
    if (done.current) return;
    done.current = true;
    exitRef.current(error, metadata);
  }, []);

  const { open, ready, error } = usePlaidLink({ token, onSuccess: handleSuccess, onExit: handleExit });

  useEffect(() => {
    if (ready && !opened.current) {
      opened.current = true;
      open();
    }
  }, [ready, open]);

  useEffect(() => {
    if (error && !done.current) {
      done.current = true;
      exitRef.current(
        {
          error_type: 'SCRIPT_LOAD_ERROR',
          error_code: 'SCRIPT_LOAD_ERROR',
          error_message: 'Couldn’t load Plaid Link. Check your internet connection and try again.',
          display_message: null,
        },
        null,
      );
    }
  }, [error]);

  return null;
}

/** Stand-in for Plaid's window in the dev mock: Continue → success, Exit → user closed Link. */
function MockPlaidLink({ onSuccess, onExit }: LauncherProps) {
  const continueRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    continueRef.current?.focus();
  }, []);
  return (
    <div className="mock-plaid" role="dialog" aria-label="Plaid Link (dev mock)">
      <div className="mock-plaid-card">
        <strong>Plaid Link (dev mock)</strong>
        <p className="small muted">In the real app, Plaid’s own window opens here.</p>
        <div className="form-actions" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn btn-ghost" onClick={() => onExit(null, null)}>
            Exit
          </button>
          <button ref={continueRef} type="button" className="btn btn-primary" onClick={() => onSuccess('public-sandbox-mock')}>
            Continue
          </button>
        </div>
      </div>
    </div>
  );
}
