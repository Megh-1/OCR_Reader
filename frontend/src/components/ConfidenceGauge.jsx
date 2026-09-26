import { motion } from 'framer-motion';
import { cn, fmtConf } from '@/lib/utils';
import { AUTO_THRESHOLD } from '@/lib/tiers';

/**
 * Confidence shown as a measurement, not a checkmark: a 0-100 scale with the field's own
 * confidence floor and the 0.90 auto line marked, and the reading between them. A value can
 * clear both lines and still be wrong - the gauge says where it sits, the tier says what that means.
 */
export function ConfidenceGauge({ value, floor, tier, size = 'md', showScale = false, className }) {
  const v = Math.max(0, Math.min(100, Number(value ?? 0)));
  const floorPct = floor != null ? Math.round(floor * 100) : null;
  const h = size === 'lg' ? 'h-3' : size === 'sm' ? 'h-1.5' : 'h-2';
  return (
    <div className={cn('flex min-w-0 items-center gap-2.5', className)}>
      <div className="relative min-w-0 flex-1">
        <div className={cn('relative w-full overflow-hidden rounded-[1px] bg-graphite-850', h)}>
          {/* decile ticks */}
          {[10, 20, 30, 40, 50, 60, 70, 80, 90].map((t) => (
            <span key={t} className="absolute top-0 h-full w-px bg-graphite-800" style={{ left: `${t}%` }} />
          ))}
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${v}%` }}
            transition={{ type: 'spring', stiffness: 120, damping: 20 }}
            className={cn(
              'absolute inset-y-0 left-0',
              tier === 'auto' && 'bg-tier-auto',
              tier === 'spot_check' && 'hatch-spot border-r-2 border-tier-spot',
              tier === 'review' && 'bg-tier-review',
              !tier && 'bg-graphite-500',
            )}
          />
        </div>
        {/* reference lines, drawn over the track */}
        {floorPct != null && (
          <span
            title={`confidence floor ${floorPct}`}
            className="absolute -top-1 -bottom-1 w-px bg-graphite-300"
            style={{ left: `${floorPct}%` }}
          />
        )}
        <span
          title={`auto line ${AUTO_THRESHOLD}`}
          className="absolute -top-1 -bottom-1 w-[2px] bg-foreground"
          style={{ left: `${AUTO_THRESHOLD}%` }}
        />
        {showScale && (
          <div className="relative mt-1.5 h-3 whitespace-nowrap font-mono text-[10px] text-faint-foreground">
            <span className="absolute left-0">0</span>
            {floorPct != null && (
              <span className="absolute -translate-x-1/2 text-secondary-foreground" style={{ left: `${floorPct}%` }}>
                floor {floorPct}
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
          'w-11 shrink-0 text-right font-mono tabular-nums',
          size === 'lg' ? 'text-[15px] font-semibold' : 'text-[12px]',
          tier === 'review' ? 'text-tier-review-text' : tier === 'spot_check' ? 'text-tier-spot-foreground' : 'text-foreground',
        )}
      >
        {fmtConf(value)}
      </span>
    </div>
  );
}
