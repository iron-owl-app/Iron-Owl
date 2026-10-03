import { passwordStrength } from '../lib/strength';

export function StrengthMeter({ password }: { password: string }) {
  const s = passwordStrength(password);
  return (
    <div className="strength" data-score={s.score}>
      <div className="strength-bars" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
      </div>
      <div className="strength-label">
        <span>{s.hint}</span>
        {s.label && <strong>{s.label}</strong>}
      </div>
    </div>
  );
}
