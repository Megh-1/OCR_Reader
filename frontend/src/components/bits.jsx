import { animate, motion, useMotionValue, useTransform } from 'framer-motion';
import { useEffect } from 'react';
import { cn } from '@/lib/utils';

/** auto / spot_check / review as one proportional strip - the same encoding everywhere. */
export function TierBar({ counts, className, height = 'h-1.5', animated = true }) {
  const total = (counts.auto ?? 0) + (counts.spot_check ?? 0) + (counts.review ?? 0);
  const segs = [
    ['auto', counts.auto ?? 0, 'bg-tier-auto'],
    ['spot_check', counts.spot_check ?? 0, 'hatch-spot bg-tier-spot-surface'],
    ['review', counts.review ?? 0, 'bg-tier-review'],
  ];
  return (
    <div
      className={cn('flex w-full gap-px overflow-hidden rounded-[1px] bg-graphite-850', height, className)}
      title={`auto ${counts.auto ?? 0} · spot check ${counts.spot_check ?? 0} · review ${counts.review ?? 0}`}
    >
      {total > 0 &&
        segs.map(([key, n, cls]) =>
          n > 0 ? (
            <motion.div
              key={key}
              className={cls}
              initial={animated ? { width: 0 } : false}
              animate={{ width: `${(100 * n) / total}%` }}
              transition={{ type: 'spring', stiffness: 140, damping: 22 }}
            />
          ) : null,
        )}
    </div>
  );
}

/** A readout that counts to its value instead of jumping. */
export function AnimatedNumber({ value, decimals = 0, className }) {
  const mv = useMotionValue(value);
  const text = useTransform(mv, (v) => v.toFixed(decimals));
  useEffect(() => {
    const controls = animate(mv, value, { duration: 0.6, ease: [0.2, 0.8, 0.2, 1] });
    return () => controls.stop();
  }, [mv, value]);
  return <motion.span className={cn('tabular-nums', className)}>{text}</motion.span>;
}

export function PageHeader({ eyebrow, title, children, meta }) {
  return (
    <header className="flex items-end justify-between gap-6 border-b border-border px-8 pt-6 pb-5">
      <div className="min-w-0">
        <div className="caps text-muted-foreground">{eyebrow}</div>
        <h1 className="mt-1.5 text-[20px] leading-7 font-semibold tracking-[-0.005em]">{title}</h1>
        {meta && <div className="mt-1 font-mono text-[12px] text-muted-foreground">{meta}</div>}
      </div>
      <div className="flex shrink-0 items-center gap-2">{children}</div>
    </header>
  );
}

export function Readout({ label, value, sub, tone = 'default', className }) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      <span className="caps-sm text-muted-foreground">{label}</span>
      <span
        className={cn(
          'font-mono text-[24px] leading-7 font-medium tracking-[-0.01em]',
          tone === 'spot' && 'text-tier-spot-foreground',
          tone === 'review' && 'text-tier-review-text',
        )}
      >
        {value}
      </span>
      {sub && <span className="font-mono text-[11px] text-faint-foreground">{sub}</span>}
    </div>
  );
}

export function EmptyState({ icon: Icon, title, children, className }) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3 px-6 py-16 text-center', className)}>
      {Icon && (
        <div className="grid size-10 place-items-center rounded-md border border-border-strong bg-muted">
          <Icon className="size-5 text-icon" />
        </div>
      )}
      <div className="text-[15px] font-semibold">{title}</div>
      {children && <div className="max-w-sm text-[13px] text-muted-foreground">{children}</div>}
    </div>
  );
}
