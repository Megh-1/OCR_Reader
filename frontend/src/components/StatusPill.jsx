import { Check, Circle, LoaderCircle, RotateCcw, TriangleAlert } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

// One status per batch document: queued and done are quiet, processing is the brand color,
// retrying is a warning, failed is the only danger.
const META = {
  queued: { label: 'Queued', Icon: Circle, tone: 'neutral' },
  processing: { label: 'Processing', Icon: LoaderCircle, tone: 'primary', spin: true },
  retrying: { label: 'Retrying', Icon: RotateCcw, tone: 'warning' },
  completed: { label: 'Done', Icon: Check, tone: 'success' },
  failed: { label: 'Failed', Icon: TriangleAlert, tone: 'danger' },
};

export function StatusPill({ status, className }) {
  const meta = META[status] ?? META.queued;
  const { Icon } = meta;
  return (
    <Badge tone={meta.tone} className={className}>
      <Icon className={cn('size-3.5', meta.spin && 'animate-spin')} aria-hidden />
      {meta.label}
    </Badge>
  );
}
