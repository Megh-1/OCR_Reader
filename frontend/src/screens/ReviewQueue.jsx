import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowDown,
  ArrowUp,
  Check,
  CheckCheck,
  CircleDashed,
  FileQuestion,
  Pencil,
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
  Zap,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  errorLines,
  getExtractionResult,
  getTemplateVersion,
  listReviewQueue,
  resolveFieldReview,
  resolveNewTemplateNeeded,
  resultImageUrl,
} from '@/api';
import { EmptyState, PageHeader } from '@/components/bits';
import { ConfidenceGauge } from '@/components/ConfidenceGauge';
import { FieldCrop } from '@/components/FieldCrop';
import { TierBadge, TierMark } from '@/components/TierBadge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle, Kbd } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { AUTO_THRESHOLD, REASONS, SPOT_WHY } from '@/lib/tiers';
import { cn, fmtConf, timeAgo } from '@/lib/utils';

const CONFIDENT = 80; // a rejected read at or above this is "confident and wrong": the case to surface first

function tierOf(entry) {
  if (entry.entry_type === 'spot_check') return 'spot_check';
  if (entry.entry_type === 'field_review') return 'review';
  return 'template';
}

function isContradiction(entry) {
  const p = entry.payload;
  if (entry.entry_type === 'field_review') {
    return (p.confidence ?? 0) >= CONFIDENT && p.reasons.some((r) => r !== 'low_confidence');
  }
  return entry.entry_type === 'spot_check' && p.why === 'no_validator' && p.confidence >= CONFIDENT;
}

// Urgency order: confident-but-rejected first, then the rest of review, then spot checks
// that only confidence vouches for, then the other spot checks, then unrecognised documents.
function urgency(entry) {
  const t = tierOf(entry);
  const conf = entry.payload.confidence ?? 0;
  if (t === 'review') return (isContradiction(entry) ? 0 : 1) * 1000 - conf;
  if (t === 'spot_check') return 2000 + (entry.payload.why === 'no_validator' ? 0 : 1000) - conf;
  return 5000;
}

export default function ReviewQueue({ onQueueChange }) {
  const [entries, setEntries] = useState([]);
  const [loadState, setLoadState] = useState('loading');
  const [selectedId, setSelectedId] = useState(null);
  const [filter, setFilter] = useState('all');
  const [resultsById, setResultsById] = useState({});
  const [stamp, setStamp] = useState(null);
  const [tally, setTally] = useState({ accepted: 0, corrected: 0 });
  const fetched = useRef(new Set());

  const load = useCallback(async () => {
    try {
      const list = await listReviewQueue('pending');
      setEntries(list);
      setLoadState('ready');
    } catch {
      setLoadState('error');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Every row wants its document's template; fetch each result (and its exact template version) once.
  useEffect(() => {
    const ids = [...new Set(entries.map((e) => e.payload.extraction_result_id).filter(Boolean))];
    for (const id of ids) {
      if (fetched.current.has(id)) continue;
      fetched.current.add(id);
      (async () => {
        try {
          const result = await getExtractionResult(id);
          const template = await getTemplateVersion(result.template_id, result.version);
          setResultsById((m) => ({ ...m, [id]: { result, template } }));
        } catch {
          setResultsById((m) => ({ ...m, [id]: { error: true } }));
        }
      })();
    }
  }, [entries]);

  const sorted = useMemo(() => [...entries].sort((a, b) => urgency(a) - urgency(b) || a.id - b.id), [entries]);
  const visible = useMemo(
    () => sorted.filter((e) => filter === 'all' || tierOf(e) === filter || (filter === 'review' && tierOf(e) === 'template')),
    [sorted, filter],
  );
  const counts = useMemo(
    () => ({
      review: entries.filter((e) => tierOf(e) === 'review').length,
      spot: entries.filter((e) => tierOf(e) === 'spot_check').length,
      template: entries.filter((e) => tierOf(e) === 'template').length,
      contradictions: entries.filter(isContradiction).length,
    }),
    [entries],
  );

  useEffect(() => {
    if (!visible.some((e) => e.id === selectedId)) setSelectedId(visible[0]?.id ?? null);
  }, [visible, selectedId]);

  const selectedIndex = visible.findIndex((e) => e.id === selectedId);
  const selected = visible[selectedIndex] ?? null;

  const move = useCallback(
    (delta) => {
      if (!visible.length) return;
      const next = Math.min(visible.length - 1, Math.max(0, selectedIndex + delta));
      setSelectedId(visible[next].id);
    },
    [visible, selectedIndex],
  );

  function handleResolved(entry, kind) {
    const idx = visible.findIndex((e) => e.id === entry.id);
    const rest = visible.filter((e) => e.id !== entry.id);
    setStamp({ kind, key: entry.id });
    setTimeout(() => setStamp(null), 900);
    setTally((t) => ({ ...t, [kind]: t[kind] + 1 }));
    setEntries((prev) => prev.filter((e) => e.id !== entry.id));
    setSelectedId(rest.length ? (rest[idx] ?? rest[rest.length - 1]).id : null);
    onQueueChange?.();
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="03 — Review queue"
        title="The human in the loop"
        meta={`${counts.review} to review · ${counts.spot} to spot-check${counts.template ? ` · ${counts.template} unrecognised` : ''}  ·  this session: ${tally.accepted} accepted, ${tally.corrected} corrected`}
      >
        <div className="flex rounded-md border border-border-strong p-0.5">
          {[
            ['all', 'All', entries.length],
            ['review', 'Review', counts.review + counts.template],
            ['spot_check', 'Spot check', counts.spot],
          ].map(([key, label, n]) => (
            <button
              key={key}
              onClick={() => setFilter(key)}
              className={cn(
                'relative flex h-7 items-center gap-2 rounded-sm px-3 text-[12px] font-medium transition-colors',
                filter === key ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {filter === key && (
                <motion.span layoutId="rq-filter" className="absolute inset-0 rounded-sm bg-accent" transition={{ duration: 0.2 }} />
              )}
              <span className="relative">{label}</span>
              <span className="relative font-mono text-[11px] text-faint-foreground">{n}</span>
            </button>
          ))}
        </div>
      </PageHeader>

      <div className="grid min-h-0 flex-1 grid-cols-[400px_minmax(0,1fr)]">
        {/* ------------------------------------------------------------------ the queue */}
        <div className="scroll-thin min-h-0 overflow-y-auto border-r border-border">
          {loadState === 'loading' && <div className="p-6 text-[13px] text-muted-foreground">Loading…</div>}
          {loadState === 'error' && <div className="p-6 text-[13px] text-tier-review-text">Couldn't reach the review queue.</div>}
          {loadState === 'ready' && entries.length === 0 && (
            <EmptyState icon={CheckCheck} title="Queue is clear">
              Nothing is waiting for a person. Everything the pipeline produced was either auto-approved or already
              resolved.
            </EmptyState>
          )}
          <QueueSections visible={visible} selectedId={selectedId} onSelect={setSelectedId} resultsById={resultsById} />
        </div>

        {/* ------------------------------------------------------------------ the item */}
        <div className="relative min-h-0 overflow-hidden">
          <AnimatePresence>
            {stamp && (
              <motion.div
                key={stamp.key}
                initial={{ opacity: 0, scale: 1.4, rotate: -8 }}
                animate={{ opacity: 1, scale: 1, rotate: -4 }}
                exit={{ opacity: 0, scale: 0.9 }}
                transition={{ type: 'spring', stiffness: 400, damping: 22 }}
                className="pointer-events-none absolute top-24 right-10 z-20 rounded-md border-2 border-foreground px-4 py-2 font-mono text-[18px] font-semibold tracking-[0.2em] text-foreground uppercase"
              >
                {stamp.kind === 'accepted' ? '✓ Accepted' : '✎ Corrected'}
              </motion.div>
            )}
          </AnimatePresence>
          {/* No mode="wait": that serializes exit-then-enter, which left the pane visibly
              blank for the ~200ms exit animation on every row switch - jarring when clicking
              through many items quickly. Overlapping enter/exit removes the gap entirely. */}
          <AnimatePresence>
            {selected ? (
              <motion.div
                key={selected.id}
                initial={{ opacity: 0, x: 24 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -24 }}
                transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
                className="scroll-thin h-full overflow-y-auto"
              >
                {selected.entry_type === 'new_template_needed' ? (
                  <TemplateNeededDetail entry={selected} onResolved={() => handleResolved(selected, 'corrected')} />
                ) : (
                  <FieldDetail
                    entry={selected}
                    context={resultsById[selected.payload.extraction_result_id]}
                    position={`${selectedIndex + 1} / ${visible.length}`}
                    onMove={move}
                    onResolved={(kind) => handleResolved(selected, kind)}
                  />
                )}
              </motion.div>
            ) : (
              loadState === 'ready' &&
              entries.length > 0 && <EmptyState title="Nothing in this filter" />
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}

function QueueSections({ visible, selectedId, onSelect, resultsById }) {
  const groups = [
    ['contradiction', 'Confident but rejected', visible.filter((e) => tierOf(e) === 'review' && isContradiction(e))],
    ['review', 'Review', visible.filter((e) => tierOf(e) === 'review' && !isContradiction(e))],
    ['unverified', 'Spot check · confidence only', visible.filter((e) => tierOf(e) === 'spot_check' && e.payload.why === 'no_validator')],
    ['spot', 'Spot check · under 0.90', visible.filter((e) => tierOf(e) === 'spot_check' && e.payload.why !== 'no_validator')],
    ['template', 'Unrecognised documents', visible.filter((e) => tierOf(e) === 'template')],
  ];
  return groups.map(([key, title, items]) =>
    items.length === 0 ? null : (
      <section key={key}>
        <div
          className={cn(
            'sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-card/95 px-4 h-8 backdrop-blur',
            key === 'contradiction' && 'text-tier-review-text',
          )}
        >
          {key === 'contradiction' && <Zap className="size-3.5 fill-current" />}
          <span className={cn('caps', key === 'contradiction' ? '' : 'text-muted-foreground')}>{title}</span>
          <span className="font-mono text-[11px] text-faint-foreground">{items.length}</span>
        </div>
        <AnimatePresence initial={false}>
          {items.map((e) => (
            <QueueRow
              key={e.id}
              entry={e}
              selected={e.id === selectedId}
              onClick={() => onSelect(e.id)}
              context={resultsById[e.payload.extraction_result_id]}
            />
          ))}
        </AnimatePresence>
      </section>
    ),
  );
}

function QueueRow({ entry, selected, onClick, context }) {
  const t = tierOf(entry);
  const p = entry.payload;
  const exit = { opacity: 0, x: -40, height: 0, transition: { duration: 0.28, ease: [0.4, 0, 1, 1] } };
  if (t === 'template') {
    return (
      <motion.button layout exit={exit} onClick={onClick} className={rowCls(selected, 'border-l-border-strong')}>
        <FileQuestion className="size-4 shrink-0 text-icon-muted" />
        <div className="min-w-0 flex-1 text-left">
          <div className="font-mono text-[12.5px]">{p.attempted_type_hint ?? 'no type hint'}</div>
          <div className="font-mono text-[11px] text-faint-foreground">{p.rejection_reason} · {timeAgo(entry.created_at)}</div>
        </div>
      </motion.button>
    );
  }
  const template = context?.result ? `${context.result.template_id}@v${context.result.version}` : '…';
  if (t === 'review') {
    const contra = isContradiction(entry);
    return (
      <motion.button
        layout
        exit={exit}
        onClick={onClick}
        className={cn(
          rowCls(selected, 'border-l-tier-review'),
          'border-l-[3px] py-3',
          // Only the confident-but-rejected group carries the loud red field - it's already its own
          // section with its own heading. Tinting every plain review row the same way made the whole
          // list read as "everything is on fire," burying the one signal that should stand out.
          contra
            ? selected
              ? 'bg-tier-review-surface'
              : 'bg-tier-review-surface/35 hover:bg-tier-review-surface/60'
            : selected
              ? 'bg-accent'
              : 'hover:bg-muted',
        )}
      >
        <div className="min-w-0 flex-1 text-left">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[13px] font-semibold">{p.field_id}</span>
            {contra && (
              <span className="caps-sm flex items-center gap-1 rounded-sm bg-tier-review px-1.5 py-0.5 text-tier-review-foreground">
                <Zap className="size-3 fill-current" /> confident
              </span>
            )}
          </div>
          <div className="mt-1 truncate font-mono text-[12.5px] text-signal-300">{p.original_value || '∅ empty read'}</div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {p.reasons.map((r) => (
              <span key={r} className="caps-sm rounded-sm border border-tier-review/60 px-1.5 py-0.5 text-tier-review-text">
                {REASONS[r] ?? r}
              </span>
            ))}
            <span className="font-mono text-[10.5px] text-faint-foreground">{template}</span>
          </div>
        </div>
        <span className={cn('font-mono text-[15px] font-semibold tabular-nums', contra ? 'text-tier-review-text' : 'text-foreground')}>
          {fmtConf(p.confidence)}
        </span>
      </motion.button>
    );
  }
  return (
    <motion.button layout exit={exit} onClick={onClick} className={cn(rowCls(selected, 'border-l-tier-spot-border'), 'py-2')}>
      <TierMark tier="spot_check" />
      <div className="min-w-0 flex-1 text-left">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-[12px] text-secondary-foreground">{p.field_id}</span>
          {p.why === 'no_validator' && <ShieldOff className="size-3 shrink-0 text-tier-spot" aria-label="no validator" />}
        </div>
        <div className="truncate font-mono text-[12px] text-foreground">{p.original_value}</div>
      </div>
      <span className="font-mono text-[12px] text-tier-spot-foreground tabular-nums">{fmtConf(p.confidence)}</span>
    </motion.button>
  );
}

function rowCls(selected, borderColor) {
  return cn(
    'relative flex w-full items-center gap-3 overflow-hidden border-b border-l-2 border-border px-4 transition-colors',
    borderColor,
    selected ? 'bg-accent' : 'hover:bg-muted',
  );
}

function checkRegex(regex, value) {
  if (!regex) return 'none';
  try {
    return new RegExp(regex).test(value.trim()) ? 'pass' : 'fail';
  } catch {
    return 'unknown';
  }
}

function FieldDetail({ entry, context, position, onMove, onResolved }) {
  const p = entry.payload;
  const tier = tierOf(entry);
  const [mode, setMode] = useState('view'); // view | correct
  const [value, setValue] = useState(p.original_value);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  const field = context?.template?.fields.find((f) => f.id === p.field_id);
  const confidence = p.confidence ?? context?.result?.fields?.[p.field_id]?.confidence ?? null;
  const contra = isContradiction(entry);

  const submit = useCallback(
    async (confirmed, kind) => {
      setBusy(true);
      setError(null);
      try {
        await resolveFieldReview(entry.id, confirmed);
        onResolved(kind);
      } catch (err) {
        setError(errorLines(err).join('; '));
        setBusy(false);
      }
    },
    [entry.id, onResolved],
  );

  useEffect(() => {
    function onKey(e) {
      if (e.target instanceof HTMLInputElement || e.metaKey || e.ctrlKey) return;
      if (e.key === 'a') submit(p.original_value, 'accepted');
      else if (e.key === 'c') {
        e.preventDefault();
        setMode('correct');
      } else if (e.key === 'j' || e.key === 'ArrowDown') onMove(1);
      else if (e.key === 'k' || e.key === 'ArrowUp') onMove(-1);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [submit, onMove, p.original_value]);

  useEffect(() => {
    if (mode === 'correct') inputRef.current?.select();
  }, [mode]);

  const regex = field ? checkRegex(field.validation_regex, p.original_value) : 'unknown';
  const checks = [
    {
      label: 'Format',
      detail: field?.validation_regex ?? 'no regex',
      state: p.reasons?.includes('format_invalid') ? 'fail' : regex,
    },
    {
      label: 'Check digit',
      detail: field?.checksum ?? 'no checksum',
      state: p.reasons?.includes('check_digit_invalid') ? 'fail' : field?.checksum ? 'pass' : 'none',
    },
    {
      label: 'Confidence floor',
      detail: field ? `≥ ${Math.round(field.confidence_floor * 100)}` : '',
      state: p.reasons?.includes('low_confidence') ? 'fail' : 'pass',
    },
    {
      label: 'Auto line',
      detail: `≥ ${AUTO_THRESHOLD} and validated`,
      state: tier === 'spot_check' ? 'fail' : confidence != null && confidence >= AUTO_THRESHOLD ? 'pass' : 'fail',
    },
  ];

  return (
    <div className="flex flex-col gap-5 p-6">
      <div className="flex items-center gap-3">
        <TierBadge tier={tier} />
        <h2 className="font-mono text-[20px] font-semibold tracking-[-0.01em]">{p.field_id}</h2>
        <span className="text-[13px] text-muted-foreground">{field?.label}</span>
        <div className="ml-auto flex items-center gap-2">
          <span className="font-mono text-[12px] text-faint-foreground">
            {context?.result ? `${context.result.template_id}@v${context.result.version} · result #${p.extraction_result_id}` : ''}
          </span>
          <span className="font-mono text-[12px] text-muted-foreground">{position}</span>
          <Button variant="outline" size="icon" onClick={() => onMove(-1)} aria-label="Previous">
            <ArrowUp />
          </Button>
          <Button variant="outline" size="icon" onClick={() => onMove(1)} aria-label="Next">
            <ArrowDown />
          </Button>
        </div>
      </div>

      {(contra || tier === 'spot_check') && (
        <motion.div
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          className={cn(
            'flex items-start gap-3 rounded-md border px-4 py-3',
            contra && tier === 'review' && 'border-tier-review bg-tier-review-surface',
            tier === 'spot_check' && 'border-tier-spot-border bg-tier-spot-surface',
          )}
        >
          {tier === 'review' ? (
            <Zap className="mt-0.5 size-4 shrink-0 fill-signal text-signal" />
          ) : p.why === 'no_validator' ? (
            <ShieldOff className="mt-0.5 size-4 shrink-0 text-tier-spot" />
          ) : (
            <CircleDashed className="mt-0.5 size-4 shrink-0 text-tier-spot" />
          )}
          <div className="text-[13px] leading-5">
            {tier === 'review' ? (
              <>
                <span className="font-semibold text-signal-300">Confident and wrong.</span>{' '}
                <span className="text-secondary-foreground">
                  The OCR is {fmtConf(confidence)} sure of this read, and it still fails{' '}
                  {p.reasons.filter((r) => r !== 'low_confidence').map((r) => REASONS[r]?.toLowerCase() ?? r).join(' and ')}.
                  A value can look right and be wrong — this one was caught before it shipped.
                </span>
              </>
            ) : p.why === 'no_validator' ? (
              <>
                <span className="font-semibold text-tier-spot-foreground">Nothing but confidence vouches for this.</span>{' '}
                <span className="text-secondary-foreground">
                  {p.field_id} has no regex and no checksum, so a plausible misread at {fmtConf(confidence)} passes every
                  check there is. Every wrong value the pipeline has ever accepted looked like this.
                </span>
              </>
            ) : (
              <>
                <span className="font-semibold text-tier-spot-foreground">Accepted, not trusted.</span>{' '}
                <span className="text-secondary-foreground">
                  It passed its checks, but at {fmtConf(confidence)} it sits under the {AUTO_THRESHOLD} auto line.
                </span>
              </>
            )}
          </div>
        </motion.div>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)_380px] gap-5">
        {/* source */}
        <Card className="overflow-hidden">
          <CardHeader>
            <CardTitle>Source · what the machine saw</CardTitle>
            <span className="font-mono text-[11px] text-faint-foreground">
              {field ? `bbox ${field.bbox.map((n) => n.toFixed(3)).join('  ')}` : ''}
            </span>
          </CardHeader>
          <div className="bed-grid p-5">
            <div className="overflow-hidden rounded-sm bg-paper shadow-[0_12px_32px_-4px_rgba(0,0,0,0.55)]">
              {field ? (
                <FieldCrop src={resultImageUrl(p.extraction_result_id)} bbox={field.bbox} tier={tier} />
              ) : (
                <div className="h-40 animate-pulse bg-muted" />
              )}
            </div>
            {field && (
              <div className="mt-4 flex items-end gap-4">
                <div className="w-[150px] shrink-0 overflow-hidden rounded-sm opacity-90">
                  <FieldCrop src={resultImageUrl(p.extraction_result_id)} bbox={field.bbox} tier={tier} mode="page" />
                </div>
                <div className="pb-1 text-[12px] text-muted-foreground">
                  Where it sits on the page. The crop above is the exact region Module 6 read — the stored render of
                  this page, not a re-scan.
                </div>
              </div>
            )}
          </div>
        </Card>

        {/* reading + decision */}
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Extracted value</CardTitle>
              <span className="font-mono text-[11px] text-faint-foreground">{timeAgo(entry.created_at)}</span>
            </CardHeader>
            <div className="flex flex-col gap-5 p-4">
              <div
                className={cn(
                  'rounded-md border bg-well px-3 py-3 font-mono text-[22px] leading-7 font-medium break-words shadow-[inset_0_1px_2px_rgba(0,0,0,0.6)]',
                  tier === 'review' ? 'border-tier-review/60 text-signal-300' : 'border-border-strong text-foreground',
                )}
              >
                {p.original_value || <span className="text-faint-foreground">∅ empty read</span>}
              </div>
              <div>
                <div className="caps-sm mb-2 text-muted-foreground">Confidence</div>
                <ConfidenceGauge value={confidence} floor={field?.confidence_floor} tier={tier} size="lg" showScale />
              </div>
              <div className="flex flex-col">
                {checks.map((c) => (
                  <div key={c.label} className="flex items-center gap-3 border-t border-border py-2 first:border-t-0">
                    {c.state === 'pass' && <ShieldCheck className="size-4 text-icon" />}
                    {c.state === 'fail' && <ShieldAlert className={cn('size-4', tier === 'review' ? 'text-signal' : 'text-tier-spot')} />}
                    {(c.state === 'none' || c.state === 'unknown') && <ShieldOff className="size-4 text-icon-muted" />}
                    <span className="w-28 text-[13px]">{c.label}</span>
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-faint-foreground">{c.detail}</span>
                    <span
                      className={cn(
                        'caps-sm',
                        c.state === 'pass' && 'text-muted-foreground',
                        c.state === 'fail' && (tier === 'review' ? 'text-tier-review-text' : 'text-tier-spot-foreground'),
                        (c.state === 'none' || c.state === 'unknown') && 'text-faint-foreground',
                      )}
                    >
                      {c.state === 'none' ? 'absent' : c.state}
                    </span>
                  </div>
                ))}
              </div>
              {tier === 'spot_check' && (
                <div className="font-mono text-[11px] text-faint-foreground">{SPOT_WHY[p.why]}</div>
              )}
            </div>
          </Card>

          <Card className="p-4">
            <AnimatePresence mode="wait" initial={false}>
              {mode === 'view' ? (
                <motion.div key="view" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex flex-col gap-2">
                  <div className="flex gap-2">
                    <Button variant="primary" size="lg" className="flex-1" disabled={busy} onClick={() => submit(p.original_value, 'accepted')}>
                      <Check /> Accept value <Kbd className="ml-1 border-graphite-400 bg-transparent text-primary-foreground">A</Kbd>
                    </Button>
                    <Button variant="secondary" size="lg" className="flex-1" disabled={busy} onClick={() => setMode('correct')}>
                      <Pencil /> Correct <Kbd className="ml-1">C</Kbd>
                    </Button>
                  </div>
                  <div className="flex items-center justify-center gap-1.5 text-[11px] text-faint-foreground">
                    <Kbd>J</Kbd>
                    <Kbd>K</Kbd> to move through the queue
                  </div>
                </motion.div>
              ) : (
                <motion.form
                  key="correct"
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex flex-col gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    submit(value, value === p.original_value ? 'accepted' : 'corrected');
                  }}
                >
                  <span className="caps text-muted-foreground">Correct value</span>
                  <Input
                    ref={inputRef}
                    mono
                    className="h-10 text-[15px]"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    onKeyDown={(e) => e.key === 'Escape' && setMode('view')}
                  />
                  {field?.validation_regex && (
                    <span className="font-mono text-[11px] text-faint-foreground">
                      must match {field.validation_regex} ·{' '}
                      <span className={checkRegex(field.validation_regex, value) === 'pass' ? 'text-secondary-foreground' : 'text-tier-review-text'}>
                        {checkRegex(field.validation_regex, value) === 'pass' ? 'matches' : 'does not match'}
                      </span>
                    </span>
                  )}
                  <div className="flex gap-2">
                    <Button type="submit" variant="primary" className="flex-1" disabled={busy}>
                      <Check /> Save correction <Kbd className="ml-1 border-graphite-400 bg-transparent text-primary-foreground">↵</Kbd>
                    </Button>
                    <Button type="button" variant="ghost" onClick={() => setMode('view')}>
                      Cancel
                    </Button>
                  </div>
                </motion.form>
              )}
            </AnimatePresence>
            {error && <div className="mt-2 text-[12px] text-tier-review-text">{error}</div>}
          </Card>
        </div>
      </div>
    </div>
  );
}

function TemplateNeededDetail({ entry, onResolved }) {
  const p = entry.payload;
  const [id, setId] = useState('');
  const [error, setError] = useState(null);
  async function submit(e) {
    e.preventDefault();
    try {
      await resolveNewTemplateNeeded(entry.id, id.trim());
      onResolved();
    } catch (err) {
      setError(errorLines(err).join('; '));
    }
  }
  return (
    <div className="flex max-w-xl flex-col gap-5 p-6">
      <div className="flex items-center gap-3">
        <FileQuestion className="size-5 text-icon" />
        <h2 className="text-[18px] font-semibold">Unrecognised document</h2>
      </div>
      <p className="text-[13px] text-muted-foreground">
        Module 5 could not confirm any template ({p.rejection_reason}) so nothing was extracted — an honest refusal
        instead of a guess. Build the template in the designer, then name it here to close this out.
      </p>
      <div className="grid grid-cols-2 gap-4 font-mono text-[12px]">
        <div>
          <div className="caps-sm mb-1 text-muted-foreground">Type hint given</div>
          {p.attempted_type_hint ?? '— none'}
        </div>
        <div>
          <div className="caps-sm mb-1 text-muted-foreground">Rejection</div>
          {p.rejection_reason}
        </div>
      </div>
      <form className="flex gap-2" onSubmit={submit}>
        <Input mono placeholder="created template id, e.g. air_waybill" value={id} onChange={(e) => setId(e.target.value)} />
        <Button type="submit" variant="primary" disabled={!id.trim()}>
          Resolve
        </Button>
      </form>
      {error && <div className="text-[12px] text-tier-review-text">{error}</div>}
    </div>
  );
}
