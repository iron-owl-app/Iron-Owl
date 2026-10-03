import { forwardRef } from 'react';

/** The green strip when nothing needs the user: "You're all caught up. Nothing needs you right now." */
export const CaughtUp = forwardRef<HTMLDivElement>(function CaughtUp(_props, ref) {
  return (
    <div className="home-caught-up" ref={ref} tabIndex={-1}>
      <span className="home-caught-up-mark" aria-hidden="true">
        ✓
      </span>
      <p>
        <strong>You’re all caught up.</strong> <span className="home-caught-up-sub">Nothing needs you right now.</span>
      </p>
    </div>
  );
});
