import { cn } from '@/lib/utils';
import { TIER_TAGLINE, TIERS } from '@/lib/tiers';

/** The tier mark alone: filled neutral square (auto), hollow orange (spot_check), solid orange (review). */
export function TierMark({ tier, className }) {
  return (
    <span
      aria-hidden
      className={cn(
        'inline-block size-2 shrink-0 rounded-[1px]',
        tier === 'auto' && 'bg-tier-auto',
        tier === 'spot_check' && 'border-[1.5px] border-tier-spot',
        tier === 'review' && 'bg-tier-review shadow-[0_0_8px_var(--signal-500)]',
        !TIERS[tier] && 'border border-dashed border-faint-foreground',
        className,
      )}
    />
  );
}

/**
 * Intensity is the message: auto carries no accent at all, spot_check a hollow mark on a faint
 * warm surface, review the only solid fill in the product. Never identical rows with different words.
 */
export function TierBadge({ tier, size = 'md', className, showTagline = false }) {
  const meta = TIERS[tier];
  const tagline = TIER_TAGLINE[tier];
  return (
    <span
      title={tagline ? `${meta.label} · ${tagline}` : undefined}
      className={cn(
        'caps inline-flex shrink-0 items-center gap-1.5 rounded-sm border',
        size === 'sm' ? 'h-5 px-1.5 text-[10px]' : 'h-6 px-2',
        tier === 'auto' && 'border-border-strong text-tier-auto-foreground',
        tier === 'spot_check' && 'border-tier-spot-border bg-tier-spot-surface text-tier-spot-foreground',
        tier === 'review' && 'border-tier-review bg-tier-review text-tier-review-foreground',
        !meta && 'border-border text-faint-foreground',
        className,
      )}
    >
      {tier === 'review' ? (
        <span aria-hidden className="inline-block size-2 rounded-[1px] bg-tier-review-foreground" />
      ) : (
        <TierMark tier={tier} />
      )}
      {meta ? meta.label : 'untiered'}
      {showTagline && tagline && <span className="opacity-70">· {tagline}</span>}
    </span>
  );
}
