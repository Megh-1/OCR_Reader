import { Check, CircleHelp, Eye, TriangleAlert } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { TIER_TAGLINE, TIER_TONE, TIERS } from '@/lib/tiers';
import { cn } from '@/lib/utils';

const TIER_ICON = { auto: Check, spot_check: Eye, review: TriangleAlert };
const DOT_TONE = { success: 'bg-success-solid', warning: 'bg-warning-solid', danger: 'bg-danger-solid' };

/** A small colored dot for dense lists; always sits next to the tier's name. */
export function TierMark({ tier, className }) {
  return (
    <span
      aria-hidden
      className={cn('inline-block size-2 shrink-0 rounded-full', DOT_TONE[TIER_TONE[tier]] ?? 'bg-border-strong', className)}
    />
  );
}

/** The tier as a pill: tone + icon + label, so it never relies on color alone. */
export function TierBadge({ tier, size = 'md', className, showTagline = false }) {
  const meta = TIERS[tier];
  const tagline = TIER_TAGLINE[tier];
  const Icon = TIER_ICON[tier] ?? CircleHelp;
  return (
    <Badge
      tone={TIER_TONE[tier] ?? 'neutral'}
      size={size === 'sm' ? 'sm' : 'md'}
      title={tagline ? `${meta.label}: ${tagline}` : undefined}
      className={className}
    >
      <Icon className="size-3.5" aria-hidden />
      {meta ? meta.label : 'Not rated'}
      {showTagline && tagline && <span className="font-normal">· {tagline}</span>}
    </Badge>
  );
}
