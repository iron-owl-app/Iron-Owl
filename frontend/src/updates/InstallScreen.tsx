import { useEffect, useId, useRef, type RefObject } from 'react';
import '../components/gate/gate.css';
import { useUpdates, type UpdateFlow } from './UpdateProvider';
import { CodeText } from './CodeText';
import {
  FAILED,
  HELP,
  INSTALLING,
  ROLLING_BACK,
  STEP_ORDER,
  failedDetails,
  helpDetails,
  progressPercent,
  stepLabel,
} from './copy';

/**
 * The full-window update screens (design D5 states 3, 5 and 6, and "Undoing the update"),
 * shown by Gate instead of the app or the password screen while the flow needs them. They
 * don't need a session: FinTrack restarts in the middle of state 3.
 */
export function InstallScreen() {
  const { flow, contact, backToApp } = useUpdates();
  const titleId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);

  // A new screen: move focus to its title so it's read out.
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [flow.kind]);

  if (flow.kind === 'progress') return <Progress flow={flow} titleId={titleId} headingRef={headingRef} />;

  if (flow.kind === 'rollingBack') {
    return (
      <div className="gate upd-gate">
        <main className="gate-card gate-w-520 upd-card" aria-labelledby={titleId} aria-busy="true">
          <div>
            <h1 id={titleId} ref={headingRef} tabIndex={-1} className="gate-title">
              {ROLLING_BACK.title}
            </h1>
            <p className="upd-card-sub">{ROLLING_BACK.body}</p>
          </div>
          <div className="upd-bar is-indeterminate" role="progressbar" aria-labelledby={titleId}>
            <span />
          </div>
        </main>
      </div>
    );
  }

  if (flow.kind === 'failed' || flow.kind === 'help') {
    const failed = flow.kind === 'failed';
    return (
      <div className="gate upd-gate">
        <main className="gate-card gate-w-540 upd-card" aria-labelledby={titleId}>
          <span className={`upd-ic52 ${failed ? 'is-amber' : 'is-info'}`} aria-hidden="true">
            {failed ? '!' : '?'}
          </span>
          <div>
            <h1 id={titleId} ref={headingRef} tabIndex={-1} className="gate-title">
              {failed ? FAILED.title : HELP.title(contact)}
            </h1>
            <p className="upd-card-body">{failed ? FAILED.body(contact) : HELP.body(contact)}</p>
          </div>
          <button type="button" className="upd-btn upd-btn-52 upd-btn-primary upd-self-start" onClick={backToApp}>
            {failed ? FAILED.back : HELP.back}
          </button>
          <details className="upd-details">
            <summary>Details</summary>
            <p>
              <CodeText
                text={
                  flow.kind === 'failed'
                    ? failedDetails(flow.code, flow.at, flow.backupKept)
                    : helpDetails(flow.code, flow.reason, flow.version)
                }
              />
            </p>
          </details>
        </main>
      </div>
    );
  }
  return null;
}

function Progress({
  flow,
  titleId,
  headingRef,
}: {
  flow: Extract<UpdateFlow, { kind: 'progress' }>;
  titleId: string;
  headingRef: RefObject<HTMLHeadingElement>;
}) {
  const current = STEP_ORDER.indexOf(flow.step);
  const pct = progressPercent(flow.step, flow.finished);
  return (
    <div className="gate upd-gate">
      <main className="gate-card gate-w-520 upd-card upd-progress" aria-labelledby={titleId} aria-busy={!flow.finished}>
        <div>
          <h1 id={titleId} ref={headingRef} tabIndex={-1} className="gate-title">
            {INSTALLING.title(flow.toVersion)}
          </h1>
          <p className="upd-card-sub">{INSTALLING.body(flow.minutes)}</p>
        </div>
        <ol className="upd-steps">
          {STEP_ORDER.map((s, i) => {
            const done = flow.finished || i < current;
            const now = !done && i === current;
            return (
              <li key={s} className={done ? 'is-done' : now ? 'is-current' : 'is-todo'} aria-current={now ? 'step' : undefined}>
                <span className="upd-step-mark" aria-hidden="true">
                  {done ? '✓' : i + 1}
                </span>
                <span>
                  {stepLabel(s, done)}
                  {done && <span className="sr-only"> (done)</span>}
                </span>
              </li>
            );
          })}
        </ol>
        <div
          className="upd-bar"
          role="progressbar"
          aria-label={INSTALLING.bar}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
          aria-valuetext={flow.finished ? `${pct}%` : INSTALLING.stepNews(flow.step)}
        >
          <span style={{ width: `${pct}%` }} />
        </div>
        <p className="sr-only" role="status">
          {flow.finished ? '' : INSTALLING.stepNews(flow.step)}
        </p>
      </main>
    </div>
  );
}
