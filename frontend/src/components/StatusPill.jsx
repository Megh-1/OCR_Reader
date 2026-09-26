import { AnimatePresence, motion } from 'framer-motion';
import { Check, Circle, LoaderCircle, RotateCcw, TriangleAlert } from 'lucide-react';
import { cn } from '@/lib/utils';

// Batch document status on the same intensity scale as the tiers:
// queued / processing / completed stay neutral, retrying reads as a spot check, failed as review.
const META = {
  queued: { label: 'Queued', Icon: Circle, cls: 'border-border text-muted-foreground', icon: 'text-status-queued' },
  processing: {
    label: 'Processing',
    Icon: LoaderCircle,
    cls: 'border-graphite-600 text-foreground scanning',
    icon: 'text-status-processing animate-spin',
  },
  retrying: {
    label: 'Retrying',
    Icon: RotateCcw,
    cls: 'border-tier-spot-border bg-tier-spot-surface text-tier-spot-foreground',
    icon: 'text-status-retrying',
  },
  completed: { label: 'Done', Icon: Check, cls: 'border-border-strong text-secondary-foreground', icon: 'text-status-completed' },
  failed: {
    label: 'Failed',
    Icon: TriangleAlert,
    cls: 'border-tier-review bg-tier-review text-tier-review-foreground',
    icon: 'text-tier-review-foreground',
  },
};

export function StatusPill({ status, className }) {
  const meta = META[status] ?? META.queued;
  const { Icon } = meta;
  return (
    <motion.span
      layout
      className={cn(
        'caps inline-flex h-6 items-center gap-1.5 overflow-hidden rounded-sm border px-2 transition-colors duration-300',
        meta.cls,
        className,
      )}
    >
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={status}
          initial={{ opacity: 0, scale: 0.4, rotate: -90 }}
          animate={{ opacity: 1, scale: 1, rotate: 0 }}
          exit={{ opacity: 0, scale: 0.4 }}
          transition={{ type: 'spring', stiffness: 500, damping: 28 }}
          className="inline-flex"
        >
          <Icon className={cn('size-3.5', meta.icon)} strokeWidth={2.25} />
        </motion.span>
      </AnimatePresence>
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={status}
          initial={{ y: 8, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: -8, opacity: 0 }}
          transition={{ duration: 0.18 }}
        >
          {meta.label}
        </motion.span>
      </AnimatePresence>
    </motion.span>
  );
}
