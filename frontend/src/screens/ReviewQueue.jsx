import { ArrowDown, ArrowUp, Check, CheckCheck, FileQuestion, Info, Pencil, ShieldAlert, ShieldCheck, ShieldOff } from 'lucide-react';
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
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle, Kbd } from '@/components/ui/card';
import { Input, Label } from '@/components/ui/input';
import { AUTO_THRESHOLD, reasonText, SPOT_WHY } from '@/lib/tiers';
import { cn, fmtConf, timeAgo } from '@/lib/utils';

const CONFIDENT = 80; // a rejected read at or above this is "confident but failed a check": the case to surface first

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

// Urgency order: confident-but-failed first, then the rest of review, then spot checks
// that only confidence vouches for, then the other spot checks, then unrecognized documents.
function urgency(entry) {
  const t = tierOf(entry);
  const conf = entry.payload.confidence ?? 0;
  if (t === 'review') return (isContradiction(entry) ? 0 : 1) * 1000 - conf;
  if (t === 'spot_check') return 2000 + (entry.payload.why === 'no_validator' ? 0 : 1000) - conf;
  return 5000;
}

const FILTERS = [
  ['all', 'All'],
  ['review', 'Needs review'],
  ['spot_check', 'Spot check'],
];

export default function ReviewQueue({ onQueueChange }) {
  const [entries, setEntries] = useState([]);
  const [loadState, setLoadState] = useState('loading');
  const [selectedId, setSelectedId] = useState(null);
  const [filter, setFilter] = useState('all');
  const [resultsById, setResultsById] = useState({});
  const [tally, setTally] = useState({ accepted: 0, corrected: 0 });
  const fetched = useRef(new Set());

  const load = useCallback(async () => {
    try {
      setEntries(await listReviewQueue('pending'));
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
      all: entries.length,
      review: entries.filter((e) => tierOf(e) !== 'spot_check').length,
      spot_check: entries.filter((e) => tierOf(e) === 'spot_check').length,
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
      setSelectedId(visible[Math.min(visible.length - 1, Math.max(0, selectedIndex + delta))].id);
    },
    [visible, selectedIndex],
  );

  function handleResolved(entry, kind) {
    const idx = visible.findIndex((e) => e.id === entry.id);
    const rest = visible.filter((e) => e.id !== entry.id);
    setTally((t) => ({ ...t, [kind]: t[kind] + 1 }));
    setEntries((prev) => prev.filter((e) => e.id !== entry.id));
    setSelectedId(rest.length ? (rest[idx] ?? rest[rest.length - 1]).id : null);
    onQueueChange?.();
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Review"
        meta={
          entries.length
            ? `${counts.all} value${counts.all === 1 ? '' : 's'} waiting for a person. This session: ${tally.accepted} accepted, ${tally.corrected} corrected.`
            : 'Values the system is not sure about are listed here for a person to confirm or correct.'
        }
      >
        <div role="group" aria-label="Filter the list" className="flex gap-1 rounded-md border border-border-strong bg-surface p-1">
          {FILTERS.map(([key, label]) => (
            <button
              key={key}
              aria-pressed={filter === key}
              onClick={() => setFilter(key)}
              className={cn(
                'flex h-8 items-center gap-2 rounded-sm px-3 text-sm font-medium transition-colors',
                filter === key ? 'bg-primary-surface text-primary-text' : 'text-foreground-secondary hover:bg-surface-sunken hover:text-foreground',
              )}
            >
              {label}
              <span className="text-xs">{counts[key]}</span>
            </button>
          ))}
        </div>
      </PageHeader>

      <div className="grid min-h-0 flex-1 grid-cols-[400px_minmax(0,1fr)]">
        <aside aria-label="Waiting for review" className="scroll-thin min-h-0 overflow-y-auto border-r border-border bg-surface">
          {loadState === 'loading' && <p className="p-6 text-sm text-foreground-secondary">Loading…</p>}
          {loadState === 'error' && <p role="alert" className="p-6 text-sm text-danger">Couldn't reach the server.</p>}
          {loadState === 'ready' && entries.length === 0 && (
            <EmptyState icon={CheckCheck} title="All caught up">
              Nothing is waiting for a person. Everything was either approved automatically or already handled.
            </EmptyState>
          )}
          <QueueSections visible={visible} selectedId={selectedId} onSelect={setSelectedId} resultsById={resultsById} />
        </aside>

        <div className="scroll-thin min-h-0 overflow-y-auto">
          {selected ? (
            selected.entry_type === 'new_template_needed' ? (
              <TemplateNeededDetail key={selected.id} entry={selected} onResolved={() => handleResolved(selected, 'corrected')} />
            ) : (
              <FieldDetail
                key={selected.id}
                entry={selected}
                context={resultsById[selected.payload.extraction_result_id]}
                position={`${selectedIndex + 1} of ${visible.length}`}
                onMove={move}
                onResolved={(kind) => handleResolved(selected, kind)}
              />
            )
          ) : (
            loadState === 'ready' && entries.length > 0 && <EmptyState title="Nothing in this filter" />
          )}
        </div>
      </div>
    </div>
  );
}

const GROUPS = [
  ['contradiction', 'Looked right but failed a check', (e) => tierOf(e) === 'review' && isContradiction(e)],
  ['review', 'Needs review', (e) => tierOf(e) === 'review' && !isContradiction(e)],
  ['unverified', 'Spot check · no format rule', (e) => tierOf(e) === 'spot_check' && e.payload.why === 'no_validator'],
  ['spot', 'Spot check · confidence under 90', (e) => tierOf(e) === 'spot_check' && e.payload.why !== 'no_validator'],
  ['template', 'Unrecognized documents', (e) => tierOf(e) === 'template'],
];

function QueueSections({ visible, selectedId, onSelect, resultsById }) {
  return GROUPS.map(([key, title, test]) => {
    const items = visible.filter(test);
    if (items.length === 0) return null;
    return (
      <section key={key} aria-labelledby={`group-${key}`}>
        <h2 id={`group-${key}`} className="sticky top-0 z-10 flex h-8 items-center gap-2 border-b border-border bg-surface-sunken px-4 text-xs font-medium text-foreground-secondary">
          {title}
          <Badge size="sm">{items.length}</Badge>
        </h2>
        <ul>
          {items.map((e) => (
            <li key={e.id} className="border-b border-border">
              <QueueRow entry={e} selected={e.id === selectedId} onClick={() => onSelect(e.id)} context={resultsById[e.payload.extraction_result_id]} />
            </li>
          ))}
        </ul>
      </section>
    );
  });
}

const rowClass = (selected) =>
  cn(
    'flex w-full items-start gap-3 border-l-4 px-4 py-3 text-left transition-colors',
    selected ? 'border-primary bg-primary-surface' : 'border-transparent hover:bg-surface-sunken',
  );

function QueueRow({ entry, selected, onClick, context }) {
  const t = tierOf(entry);
  const p = entry.payload;
  if (t === 'template') {
    return (
      <button onClick={onClick} aria-current={selected ? 'true' : undefined} className={rowClass(selected)}>
        <FileQuestion className="mt-1 size-4 shrink-0 text-foreground-subtle" aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-foreground">Unrecognized document</span>
          <span className="block text-xs text-foreground-secondary">
            {p.attempted_type_hint ? `Chosen type: ${p.attempted_type_hint}` : 'No type chosen'} · {timeAgo(entry.created_at)}
          </span>
        </span>
      </button>
    );
  }
  const label = context?.template?.fields.find((f) => f.id === p.field_id)?.label ?? p.field_id;
  return (
    <button onClick={onClick} aria-current={selected ? 'true' : undefined} className={rowClass(selected)}>
      <span className="mt-2">
        <TierMark tier={t} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline justify-between gap-2">
          <span className="truncate text-sm font-medium text-foreground">{label}</span>
          <span className={cn('text-sm font-medium tabular-nums', t === 'review' ? 'text-danger' : 'text-warning')}>{fmtConf(p.confidence)}</span>
        </span>
        <span className="block truncate font-mono text-xs text-foreground-secondary">{p.original_value || 'Empty'}</span>
        <span className="mt-1 flex flex-wrap items-center gap-1">
          {t === 'review' ? (
            p.reasons.map((r) => (
              <Badge key={r} tone="danger" size="sm">
                {reasonText(r)}
              </Badge>
            ))
          ) : (
            <Badge tone="warning" size="sm">
              {p.why === 'no_validator' ? 'No format rule' : 'Low confidence'}
            </Badge>
          )}
        </span>
      </span>
    </button>
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

const CHECK_ICON = { pass: ShieldCheck, fail: ShieldAlert, none: ShieldOff, unknown: ShieldOff };
const CHECK_WORD = { pass: 'Passed', fail: 'Failed', none: 'Not set', unknown: 'Not set' };

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
    { label: 'Format rule', state: p.reasons?.includes('format_invalid') ? 'fail' : regex },
    { label: 'Check digit', state: p.reasons?.includes('check_digit_invalid') ? 'fail' : field?.checksum ? 'pass' : 'none' },
    {
      label: field ? `Minimum confidence (${Math.round(field.confidence_floor * 100)})` : 'Minimum confidence',
      state: p.reasons?.includes('low_confidence') ? 'fail' : 'pass',
    },
    { label: `Auto-approval (${AUTO_THRESHOLD}+)`, state: tier === 'spot_check' ? 'fail' : confidence != null && confidence >= AUTO_THRESHOLD ? 'pass' : 'fail' },
  ];
  const failedTone = tier === 'review' ? 'text-danger' : 'text-warning';
  const valueIsEmpty = !p.original_value;

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex flex-1 flex-col gap-4 p-6">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-lg text-foreground">{field?.label ?? p.field_id}</h2>
          <TierBadge tier={tier} />
          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-foreground-secondary">
              {context?.result ? `Document #${p.extraction_result_id} · ` : ''}
              {position}
            </span>
            <Button variant="outline" size="icon" onClick={() => onMove(-1)} aria-label="Previous item">
              <ArrowUp />
            </Button>
            <Button variant="outline" size="icon" onClick={() => onMove(1)} aria-label="Next item">
              <ArrowDown />
            </Button>
          </div>
        </div>

        <div
          role="note"
          className={cn(
            'flex items-start gap-3 rounded-md border px-4 py-3 text-sm',
            tier === 'review' ? 'border-danger-border bg-danger-surface text-danger' : 'border-warning-border bg-warning-surface text-warning',
          )}
        >
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p className="text-foreground">
            {tier === 'review' ? (
              contra ? (
                <>
                  <strong>Looked right, but failed a check.</strong> The system was {fmtConf(confidence)} sure of this reading, yet it
                  {' '}failed: {p.reasons.filter((r) => r !== 'low_confidence').map((r) => reasonText(r).toLowerCase()).join(' and ')}.
                </>
              ) : (
                <>
                  <strong>This value was not accepted.</strong> {p.reasons.map(reasonText).join(' · ')}.
                </>
              )
            ) : (
              <>
                <strong>Probably right, worth a quick look.</strong> {SPOT_WHY[p.why]}
              </>
            )}
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <Card className="overflow-hidden">
            <CardHeader>
              <CardTitle>Where it was read from</CardTitle>
            </CardHeader>
            <div className="flex flex-col gap-4 bg-surface-sunken p-4">
              <div className="overflow-hidden rounded-md border border-border bg-white">
                {field ? (
                  <FieldCrop src={resultImageUrl(p.extraction_result_id)} bbox={field.bbox} tier={tier} />
                ) : (
                  <div className="h-40 animate-pulse bg-surface-sunken" />
                )}
              </div>
              {field && (
                <div className="flex items-end gap-4">
                  <div className="w-40 shrink-0 overflow-hidden rounded-sm border border-border">
                    <FieldCrop src={resultImageUrl(p.extraction_result_id)} bbox={field.bbox} tier={tier} mode="page" />
                  </div>
                  <p className="text-xs text-foreground-secondary">The small page shows where this box sits. The larger image is exactly the area the system read.</p>
                </div>
              )}
            </div>
          </Card>

          <div className="flex flex-col gap-4">
            <Card>
              <CardHeader>
                <CardTitle>What the system read</CardTitle>
                <span className="text-xs text-foreground-subtle">{timeAgo(entry.created_at)}</span>
              </CardHeader>
              <div className="flex flex-col gap-4 p-4">
                <p
                  className={cn(
                    'break-words rounded-md border bg-surface-sunken px-3 py-3 font-mono text-lg',
                    tier === 'review' ? 'border-danger-border' : 'border-border-strong',
                    valueIsEmpty ? 'font-sans text-foreground-subtle' : 'text-foreground',
                  )}
                >
                  {valueIsEmpty ? 'Nothing was read' : p.original_value}
                </p>
                <div>
                  <p className="label mb-2">Confidence</p>
                  <ConfidenceGauge value={confidence} floor={field?.confidence_floor} tier={tier} size="lg" showScale />
                </div>
                <ul>
                  {checks.map((c) => {
                    const Icon = CHECK_ICON[c.state];
                    return (
                      <li key={c.label} className="flex items-center gap-3 border-t border-border py-2 first:border-t-0">
                        <Icon className={cn('size-4 shrink-0', c.state === 'fail' ? failedTone : 'text-foreground-subtle')} aria-hidden />
                        <span className="min-w-0 flex-1 text-sm text-foreground">{c.label}</span>
                        <span className={cn('text-xs font-medium', c.state === 'fail' ? failedTone : 'text-foreground-secondary')}>{CHECK_WORD[c.state]}</span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            </Card>
          </div>
        </div>
      </div>

      <div className="sticky bottom-0 border-t border-border bg-surface p-4 shadow-md">
        <div className="mx-auto flex max-w-3xl flex-col gap-2">
          {mode === 'view' ? (
            <>
              <div className="flex gap-2">
                <Button variant="primary" size="lg" className="flex-1" disabled={busy} onClick={() => submit(p.original_value, 'accepted')}>
                  <Check /> Accept
                </Button>
                <Button variant="outline" size="lg" className="flex-1" disabled={busy} onClick={() => setMode('correct')}>
                  <Pencil /> Correct
                </Button>
              </div>
              <p className="flex flex-wrap items-center justify-center gap-1 text-xs text-foreground-subtle">
                Shortcuts: <Kbd>A</Kbd> accept · <Kbd>C</Kbd> correct · <Kbd>J</Kbd> <Kbd>K</Kbd> next / previous
              </p>
            </>
          ) : (
            <form
              className="flex flex-col gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                submit(value, value === p.original_value ? 'accepted' : 'corrected');
              }}
            >
              <Label>
                Correct value
                <Input ref={inputRef} mono value={value} onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => e.key === 'Escape' && setMode('view')} />
              </Label>
              {field?.validation_regex && (
                <p className="text-xs text-foreground-secondary">
                  {checkRegex(field.validation_regex, value) === 'pass' ? (
                    <span className="text-success">Matches the expected format.</span>
                  ) : (
                    <span className="text-danger">Doesn't match the expected format.</span>
                  )}
                </p>
              )}
              <div className="flex gap-2">
                <Button type="submit" variant="primary" className="flex-1" disabled={busy}>
                  <Check /> Save correction
                </Button>
                <Button type="button" variant="ghost" onClick={() => setMode('view')}>
                  Cancel
                </Button>
              </div>
            </form>
          )}
          {error && (
            <p role="alert" className="text-xs text-danger">
              {error}
            </p>
          )}
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
    <div className="flex max-w-xl flex-col gap-4 p-6">
      <div className="flex items-center gap-3">
        <FileQuestion className="size-5 text-foreground-secondary" aria-hidden />
        <h2 className="text-lg text-foreground">Unrecognized document</h2>
      </div>
      <p className="text-sm text-foreground-secondary">
        The system could not tell which template this document matches, so nothing was extracted. That is deliberate: it is safer to stop than to guess. Create a
        template for this layout in Templates, then enter its name below to close this item.
      </p>
      <dl className="grid grid-cols-2 gap-4 text-sm">
        <div>
          <dt className="label">Type you chose</dt>
          <dd className="text-foreground">{p.attempted_type_hint ?? 'None (automatic)'}</dd>
        </div>
        <div>
          <dt className="label">Why it was refused</dt>
          <dd className="text-foreground">{p.rejection_reason?.replaceAll('_', ' ')}</dd>
        </div>
      </dl>
      <form className="flex items-end gap-2" onSubmit={submit}>
        <Label className="flex-1">
          Name of the template you created
          <Input mono placeholder="e.g. air_waybill" value={id} onChange={(e) => setId(e.target.value)} />
        </Label>
        <Button type="submit" variant="primary" disabled={!id.trim()}>
          Resolve
        </Button>
      </form>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
