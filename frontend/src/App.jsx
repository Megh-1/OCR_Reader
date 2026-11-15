import { ChartColumnBig, Inbox, Layers3, ScanLine } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { listReviewQueue } from '@/api';
import { TierMark } from '@/components/TierBadge';
import { Badge } from '@/components/ui/badge';
import { TIER_TAGLINE, TIERS } from '@/lib/tiers';
import { useAdvanced } from '@/lib/settings';
import { cn } from '@/lib/utils';
import BatchMonitor from '@/screens/BatchMonitor';
import Results from '@/screens/Results';
import ReviewQueue from '@/screens/ReviewQueue';
import TemplateDesigner from '@/screens/TemplateDesigner';

const ROUTES = [
  { key: 'batch', label: 'Upload', Icon: ScanLine, Screen: BatchMonitor },
  { key: 'results', label: 'Results', Icon: ChartColumnBig, Screen: Results },
  { key: 'review', label: 'Review', Icon: Inbox, Screen: ReviewQueue },
  { key: 'templates', label: 'Templates', Icon: Layers3, Screen: TemplateDesigner, advanced: true },
];

function parseHash(advanced) {
  const [key, ...rest] = window.location.hash.replace(/^#\/?/, '').split('/');
  return { key: ROUTES.some((r) => r.key === key && (advanced || !r.advanced)) ? key : 'batch', param: rest.join('/') || null };
}

function NavItem({ route, active, queue }) {
  const { key, label, Icon } = route;
  const pending = key === 'review' ? queue.review + queue.spot : 0;
  return (
    <a
      href={`#/${key}`}
      aria-current={active ? 'page' : undefined}
      title={label}
      className={cn(
        'flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors',
        active ? 'bg-primary-surface text-primary-text' : 'text-foreground-secondary hover:bg-surface-sunken hover:text-foreground',
      )}
    >
      <Icon className="size-5 shrink-0" aria-hidden />
      <span className="sr-only flex-1 xl:not-sr-only">{label}</span>
      {pending > 0 && (
        <Badge tone={queue.review > 0 ? 'danger' : 'warning'} size="sm" className="hidden xl:inline-flex">
          {pending}
          <span className="sr-only"> waiting</span>
        </Badge>
      )}
    </a>
  );
}

function TierLegend() {
  return (
    <section aria-label="What the approval levels mean" className="hidden flex-col gap-2 xl:flex">
      <h2 className="label">Approval levels</h2>
      <ul className="flex flex-col gap-2 text-xs text-foreground-secondary">
        {Object.keys(TIERS).map((tier) => (
          <li key={tier} className="flex items-center gap-2">
            <TierMark tier={tier} />
            <span>
              <span className="font-medium text-foreground">{TIERS[tier].label}</span> · {TIER_TAGLINE[tier]}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function App() {
  const [advanced, toggleAdvanced] = useAdvanced();
  const [route, setRoute] = useState(() => parseHash(advanced));
  const [queue, setQueue] = useState({ review: 0, spot: 0, reachable: null });

  useEffect(() => {
    const onHash = () => setRoute(parseHash(advanced));
    onHash(); // also when the switch changes: leaving the Templates screen when it gets hidden
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, [advanced]);

  const refreshQueue = useCallback(async () => {
    try {
      const entries = await listReviewQueue('pending');
      setQueue({
        review: entries.filter((e) => e.entry_type !== 'spot_check').length,
        spot: entries.filter((e) => e.entry_type === 'spot_check').length,
        reachable: true,
      });
    } catch {
      setQueue((q) => ({ ...q, reachable: false }));
    }
  }, []);

  useEffect(() => {
    refreshQueue();
    const t = setInterval(refreshQueue, 5000);
    return () => clearInterval(t);
  }, [refreshQueue]);

  const { Screen } = ROUTES.find((r) => r.key === route.key);

  return (
    <div className="flex h-full min-h-0">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
      >
        Skip to content
      </a>

      <aside className="flex w-16 shrink-0 flex-col border-r border-border bg-surface xl:w-60">
        <div className="flex h-16 items-center gap-3 border-b border-border px-4">
          <div className="grid size-8 shrink-0 place-items-center rounded-md bg-primary text-primary-foreground">
            <ScanLine className="size-5" aria-hidden />
          </div>
          <div className="hidden min-w-0 xl:block">
            <p className="truncate text-sm font-semibold text-foreground">Extraction Console</p>
            <p className="truncate text-xs text-foreground-secondary">Air waybill processing</p>
          </div>
        </div>

        <nav aria-label="Main" className="flex flex-col gap-1 p-2 xl:p-4">
          {ROUTES.filter((r) => advanced || !r.advanced).map((r) => (
            <NavItem key={r.key} route={r} active={r.key === route.key} queue={queue} />
          ))}
        </nav>

        <div className="mt-auto flex flex-col gap-4 border-t border-border p-2 xl:p-4">
          <TierLegend />
          <label className="flex cursor-pointer items-center gap-2 text-xs text-foreground-secondary" title="Show the Templates screen and the Evidence tab">
            <input type="checkbox" checked={advanced} onChange={toggleAdvanced} className="size-3.5 shrink-0" />
            <span className="sr-only xl:not-sr-only">Advanced tools</span>
          </label>
          <p className="flex items-center gap-2 text-xs text-foreground-secondary" role="status">
            <span aria-hidden className={cn('size-2 shrink-0 rounded-full', queue.reachable === false ? 'bg-danger-solid' : 'bg-success-solid')} />
            <span className="sr-only xl:not-sr-only">{queue.reachable === false ? 'Server unreachable' : 'Server connected'}</span>
          </p>
        </div>
      </aside>

      <main id="main" className="relative min-w-0 flex-1 overflow-hidden">
        <Screen key={route.key} param={route.param} onQueueChange={refreshQueue} />
      </main>
    </div>
  );
}
