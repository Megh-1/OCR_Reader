import { motion } from 'framer-motion';
import { cn } from '@/lib/utils';

/** auto / spot_check / review as one proportional strip - the same encoding everywhere. */
export function TierBar({ counts, className, height = 'h-2' }) {
  const total = (counts.auto ?? 0) + (counts.spot_check ?? 0) + (counts.review ?? 0);
  const segs = [
    ['auto', counts.auto ?? 0, 'bg-success-solid'],
    ['spot_check', counts.spot_check ?? 0, 'bg-warning-solid'],
    ['review', counts.review ?? 0, 'bg-danger-solid'],
  ];
  const label = `${counts.auto ?? 0} auto, ${counts.spot_check ?? 0} spot check, ${counts.review ?? 0} review`;
  return (
    <div
      role="img"
      aria-label={label}
      title={label}
      className={cn('flex w-full gap-px overflow-hidden rounded-sm bg-surface-sunken', height, className)}
    >
      {total > 0 &&
        segs.map(([key, n, cls]) =>
          n > 0 ? <motion.div key={key} className={cls} initial={false} animate={{ width: `${(100 * n) / total}%` }} transition={{ duration: 0.2 }} /> : null,
        )}
    </div>
  );
}

/** A number, formatted to a fixed number of decimals. */
export function AnimatedNumber({ value, decimals = 0, className }) {
  return <span className={cn('tabular-nums', className)}>{Number(value).toFixed(decimals)}</span>;
}

export function PageHeader({ title, children, meta }) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border bg-surface px-6 py-4">
      <div className="min-w-0">
        <h1 className="text-xl text-foreground">{title}</h1>
        {meta && <p className="mt-1 max-w-prose text-sm text-foreground-secondary">{meta}</p>}
      </div>
      {children && <div className="flex max-w-full shrink-0 flex-wrap items-center gap-2">{children}</div>}
    </header>
  );
}

const READOUT_TONE = { default: 'text-foreground', spot: 'text-warning', review: 'text-danger' };

export function Readout({ label, value, sub, tone = 'default', className }) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      <span className="label">{label}</span>
      <span className={cn('text-xl font-semibold tabular-nums', READOUT_TONE[tone])}>{value}</span>
      {sub && <span className="text-xs text-foreground-subtle">{sub}</span>}
    </div>
  );
}

export function EmptyState({ icon: Icon, title, children, className }) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3 px-6 py-12 text-center', className)}>
      {Icon && (
        <div className="grid size-10 place-items-center rounded-md bg-surface-sunken">
          <Icon className="size-5 text-foreground-secondary" aria-hidden />
        </div>
      )}
      <h2 className="text-base font-medium text-foreground">{title}</h2>
      {children && <p className="max-w-sm text-sm text-foreground-secondary">{children}</p>}
    </div>
  );
}
