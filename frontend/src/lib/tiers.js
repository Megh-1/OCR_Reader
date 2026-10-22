// The approval tiers Module 7 assigns (app/modules/validation.py approval_tier), and what
// each one looks like. Kept in one place so every screen says the same thing the same way.
export const AUTO_THRESHOLD = 90; // CONFIDENCE_AUTO_APPROVE_THRESHOLD * 100 (app/config.py)

// The one line of language every screen uses for a tier - the sidebar legend, the Results
// table, TierBadge's title attribute. One source, so they can't drift out of sync.
export const TIER_TAGLINE = {
  auto: 'approved automatically',
  spot_check: 'check quickly',
  review: 'needs a person',
};

// Which data-state tone each tier uses (see ui/badge.jsx).
export const TIER_TONE = {
  auto: 'success',
  spot_check: 'warning',
  review: 'danger',
};

export const TIERS = {
  auto: {
    key: 'auto',
    label: 'Auto',
    short: 'Auto',
    blurb: 'Passed its format check and the system is confident (0.90 or higher).',
  },
  spot_check: {
    key: 'spot_check',
    label: 'Spot check',
    short: 'Spot',
    blurb: 'Looks right, but confidence is under 0.90 or there is no format rule to check it against.',
  },
  review: {
    key: 'review',
    label: 'Review',
    short: 'Review',
    blurb: 'Failed its format check, its check digit or the minimum confidence.',
  },
};

export const TIER_ORDER = ['auto', 'spot_check', 'review'];

// Module 7's rejection reasons, said plainly.
export const REASONS = {
  format_invalid: 'Wrong format',
  check_digit_invalid: 'Check digit does not match',
  low_confidence: 'Confidence too low',
  enum_missing_regex: 'Template is missing a format rule',
};

// A failed cross-field check arrives as "cross_check_failed:<the check's label>".
const CROSS_CHECK = 'cross_check_failed:';
export const reasonText = (reason) =>
  reason.startsWith(CROSS_CHECK) ? `Disagrees with other fields (${reason.slice(CROSS_CHECK.length)})` : (REASONS[reason] ?? reason);

export const SPOT_WHY = {
  below_auto_threshold: 'Confidence is under 90, so it needs a quick look.',
  no_validator: 'This field has no format rule, so confidence is the only check.',
};

export function tierCounts(fields) {
  const counts = { auto: 0, spot_check: 0, review: 0 };
  for (const f of Object.values(fields ?? {})) if (f.approval_tier in counts) counts[f.approval_tier] += 1;
  return counts;
}
