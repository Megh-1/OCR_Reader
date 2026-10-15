import { AUTO_THRESHOLD } from '@/lib/tiers';
import { cn, fmtConf } from '@/lib/utils';

const FILL = { auto: 'bg-success-solid', spot_check: 'bg-warning-solid', review: 'bg-danger-solid' };
const TEXT = { auto: 'text-foreground', spot_check: 'text-warning', review: 'text-danger' };
const HEIGHT = { sm: 'h-2', md: 'h-2', lg: 'h-3' };

/**
 * Confidence on a 0-100 bar. The thin marker is the field's minimum confidence (below it the
 * field is always flagged); the thick marker is the auto-approval line. A value can clear both
 * and still be wrong - the bar says where it sits, the approval level says what that means.
 */
export function ConfidenceGauge({ value, floor, tier, size = 'md', showScale = false, className }) {
  const v = Math.max(0, Math.min(100, Number(value ?? 0)));
  const floorPct = floor != null ? Math.round(floor * 100) : null;
  return (
    <div className={cn('flex min-w-0 items-center gap-2', className)}>
      <div className="relative min-w-0 flex-1">
        <div className={cn('w-full overflow-hidden rounded-sm bg-surface-sunken', HEIGHT[size])}>
          <div className={cn('h-full', FILL[tier] ?? 'bg-foreground-subtle')} style={{ width: `${v}%` }} />
        </div>
        {floorPct != null && (
          <span
            aria-hidden
            title={`Minimum confidence ${floorPct}`}
            className="absolute -inset-y-1 w-0 border-l border-foreground-subtle"
            style={{ left: `${floorPct}%` }}
          />
        )}
        <span
          aria-hidden
          title={`Auto-approval line ${AUTO_THRESHOLD}`}
          className="absolute -inset-y-1 w-0 border-l-2 border-foreground"
          style={{ left: `${AUTO_THRESHOLD}%` }}
        />
        {showScale && (
          <div className="relative mt-2 h-4 whitespace-nowrap text-xs text-foreground-subtle">
            <span className="absolute left-0">0</span>
            {floorPct != null && (
              <span className="absolute -translate-x-1/2 text-foreground-secondary" style={{ left: `${floorPct}%` }}>
                minimum {floorPct}
              </span>
            )}
            <span className="absolute -translate-x-1/2 text-foreground" style={{ left: `${AUTO_THRESHOLD}%` }}>
              auto {AUTO_THRESHOLD}
            </span>
          </div>
        )}
      </div>
      <span
        className={cn(
          'w-10 shrink-0 text-right font-mono tabular-nums',
          size === 'lg' ? 'text-base font-semibold' : 'text-xs',
          TEXT[tier] ?? 'text-foreground',
        )}
      >
        {fmtConf(value)}
      </span>
    </div>
  );
}
