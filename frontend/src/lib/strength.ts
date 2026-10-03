export const MIN_PASSWORD_LENGTH = 12;

export interface Strength {
  /** 0 = empty, 1 weak … 4 strong */
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
  hint: string;
}

const COMMON = ['password', 'passw0rd', 'qwerty', '123456', 'letmein', 'welcome', 'admin', 'iloveyou', 'monkey', 'dragon', 'fintrack', 'ironowl', 'iron owl', 'money', 'finance'];

/** A rough, dependency-free strength estimate. Guidance only — the server enforces length. */
export function passwordStrength(pw: string): Strength {
  if (!pw) return { score: 0, label: '', hint: `At least ${MIN_PASSWORD_LENGTH} characters. A long phrase of unrelated words works well.` };

  const len = [...pw].length;
  if (len < MIN_PASSWORD_LENGTH) {
    return { score: 1, label: 'Too short', hint: `${MIN_PASSWORD_LENGTH - len} more character${MIN_PASSWORD_LENGTH - len === 1 ? '' : 's'} needed.` };
  }

  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9\s]/, /\s/].filter((r) => r.test(pw)).length;
  const lower = pw.toLowerCase();
  const hasCommon = COMMON.some((w) => lower.includes(w));
  const repetitive = /(.)\1{2,}/.test(pw) || new Set(pw).size < len / 3;
  const sequential = /(?:abcd|bcde|cdef|1234|2345|3456|4567|5678|6789|qwer|asdf|zxcv)/i.test(pw);

  let score = len >= 20 ? 3 : len >= 16 ? 2.5 : 2;
  if (classes >= 3) score += 1;
  else if (classes >= 2) score += 0.5;
  if (hasCommon) score -= 1.5;
  if (repetitive) score -= 1;
  if (sequential) score -= 0.5;

  const s = Math.max(1, Math.min(4, Math.floor(score))) as 1 | 2 | 3 | 4;
  const labels = { 1: 'Weak', 2: 'Fair', 3: 'Good', 4: 'Strong' } as const;
  let hint = 'Nice — this would take a very long time to guess.';
  if (hasCommon) hint = 'Avoid common words like “password” or the app’s name.';
  else if (repetitive || sequential) hint = 'Avoid repeated characters and keyboard runs.';
  else if (s <= 2) hint = 'Make it longer, or mix in numbers and symbols.';
  else if (s === 3) hint = 'Good. A few more characters would make it stronger.';
  return { score: s, label: labels[s], hint };
}
