// The approval tiers Module 7 assigns (app/modules/validation.py approval_tier), and what
// each one looks like. Kept in one place so every screen says the same thing the same way.
export const AUTO_THRESHOLD = 90; // CONFIDENCE_AUTO_APPROVE_THRESHOLD * 100 (app/config.py)

export const TIERS = {
  auto: {
    key: 'auto',
    label: 'Auto',
    short: 'Auto',
    blurb: 'Accepted, validated by a regex or checksum, confidence ≥ 0.90.',
  },
  spot_check: {
    key: 'spot_check',
    label: 'Spot check',
    short: 'Spot',
    blurb: 'Accepted — but under 0.90, or nothing but confidence checked it.',
  },
  review: {
    key: 'review',
    label: 'Review',
    short: 'Review',
    blurb: 'Rejected by format, check digit or confidence floor.',
  },
};

export const TIER_ORDER = ['auto', 'spot_check', 'review'];

// Module 7's rejection reasons, said plainly.
export const REASONS = {
  format_invalid: 'Format check failed',
  check_digit_invalid: 'Check digit fails',
  low_confidence: 'Below confidence floor',
  enum_missing_regex: 'Enum without a regex',
};

export const SPOT_WHY = {
  below_auto_threshold: 'Below the 0.90 auto line',
  no_validator: 'No regex or checksum — confidence is the only check',
};

export function tierCounts(fields) {
  const counts = { auto: 0, spot_check: 0, review: 0 };
  for (const f of Object.values(fields ?? {})) if (f.approval_tier in counts) counts[f.approval_tier] += 1;
  return counts;
}
