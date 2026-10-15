import { AlertTriangle, Braces, Check, Copy, Download, FileSearch, Loader2, Pencil, RotateCcw, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { correctResultField, errorLines, getExtractionResult, getTemplateVersion, listResults } from '@/api';
import { EmptyState, PageHeader, Readout, TierBar } from '@/components/bits';
import { ConfidenceGauge } from '@/components/ConfidenceGauge';
import { TierBadge } from '@/components/TierBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { navigate } from '@/lib/nav';
import { REASONS, tierCounts } from '@/lib/tiers';
import { cn, fmtConf, timeAgo } from '@/lib/utils';

const STATUS = {
  needs_review: { label: 'Needs review', tone: 'danger' },
  complete: { label: 'Complete', tone: 'success' },
};
const TIER_RANK = { review: 0, spot_check: 1, auto: 2 };
const pct = (n, total) => `${Math.round((100 * n) / total)}%`;

export default function Results({ param }) {
  const [list, setList] = useState([]);
  const [listState, setListState] = useState('loading');
  const [detail, setDetail] = useState(null);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    listResults(80)
      .then((l) => {
        setList(l);
        setListState('ready');
      })
      .catch(() => setListState('error'));
  }, []);

  const selectedId = param ? Number(param) : list[0]?.id;

  useEffect(() => {
    if (!selectedId) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const result = await getExtractionResult(selectedId);
        const template = await getTemplateVersion(result.template_id, result.version).catch(() => null);
        if (!cancelled) setDetail({ id: selectedId, result, template });
      } catch {
        if (!cancelled) setDetail({ id: selectedId, error: true });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  async function exportAll() {
    setExporting(true);
    try {
      const results = [];
      for (let i = 0; i < list.length; i += 8) {
        const chunk = await Promise.all(list.slice(i, i + 8).map((r) => getExtractionResult(r.id).then((x) => ({ id: r.id, ...x }))));
        results.push(...chunk);
      }
      downloadJson({ exported_at: new Date().toISOString(), count: results.length, results }, `extraction-results-${new Date().toISOString().slice(0, 10)}.json`);
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Results" meta="Every document that was read, with its values as JSON. Download one, or all of them as a single file.">
        <Button variant="outline" disabled={exporting || list.length === 0} onClick={exportAll}>
          {exporting ? <Loader2 className="animate-spin" /> : <Download />} Download all (one file)
        </Button>
      </PageHeader>

      <div className="grid min-h-0 flex-1 grid-cols-[304px_minmax(0,1fr)]">
        <aside aria-label="Documents" className="scroll-thin min-h-0 overflow-y-auto border-r border-border bg-surface">
          {listState === 'ready' && list.length === 0 && (
            <EmptyState icon={FileSearch} title="No results yet">
              Upload a document and every page that is read will appear here.
            </EmptyState>
          )}
          {listState === 'error' && <EmptyState title="Couldn't load results">Check that the server is running, then reload.</EmptyState>}
          <ul>
            {list.map((r) => (
              <li key={r.id} className="border-b border-border">
                <ResultListItem result={r} selected={r.id === selectedId} />
              </li>
            ))}
          </ul>
        </aside>

        <div className="scroll-thin min-h-0 overflow-y-auto">
          {detail?.result && detail.id === selectedId ? (
            <ResultDetail key={detail.id} detail={detail} />
          ) : detail?.error ? (
            <EmptyState title="Couldn't load this result" />
          ) : listState !== 'ready' || list.length ? (
            <p className="p-6 text-sm text-foreground-secondary">Loading…</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function ResultListItem({ result: r, selected }) {
  const status = STATUS[r.overall_status] ?? STATUS.complete;
  return (
    <button
      onClick={() => navigate('results', r.id)}
      aria-current={selected ? 'true' : undefined}
      className={cn(
        'flex w-full flex-col gap-2 border-l-4 px-4 py-3 text-left transition-colors',
        selected ? 'border-primary bg-primary-surface' : 'border-transparent hover:bg-surface-sunken',
      )}
    >
      <span className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate text-sm font-medium text-foreground">
          #{r.id} · {r.template_id}
        </span>
        <Badge tone={status.tone} size="sm">
          {status.label}
        </Badge>
      </span>
      <TierBar counts={r.tier_counts} />
      <span className="flex justify-between text-xs text-foreground-secondary">
        <span>
          {r.tier_counts.auto} auto · {r.tier_counts.spot_check} spot check · {r.tier_counts.review} review
        </span>
        <span>{timeAgo(r.extracted_at)}</span>
      </span>
    </button>
  );
}

function ResultDetail({ detail }) {
  const { template, id } = detail;
  // Corrections write straight to the server; this is a `key={detail.id}`-scoped local
  // mirror of that server state, replaced wholesale on every successful write (a re-fetch,
  // not a locally-recomputed guess) so overall_status and everything else it can affect
  // stay exactly what the API actually persisted - never an optimistic update.
  const [result, setResult] = useState(detail.result);
  const [copied, setCopied] = useState(false);
  const [showJson, setShowJson] = useState(false);
  const fieldsMeta = useMemo(() => Object.fromEntries((template?.fields ?? []).map((f) => [f.id, f])), [template]);
  const fields = Object.values(result.fields);
  const counts = tierCounts(result.fields);
  const total = fields.length || 1;
  const mean = fields.reduce((sum, f) => sum + f.confidence, 0) / total;
  const json = JSON.stringify(result, null, 2);
  const status = STATUS[result.overall_status] ?? STATUS.complete;

  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg text-foreground">
          Document #{id} <span className="font-normal text-foreground-secondary">· {result.template_id} v{result.version}</span>
        </h2>
        <Badge tone={status.tone}>{status.label}</Badge>
        <span className="text-xs text-foreground-subtle">{new Date(result.extracted_at).toLocaleString()}</span>
        <div className="ml-auto flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              navigator.clipboard.writeText(json);
              setCopied(true);
              setTimeout(() => setCopied(false), 1400);
            }}
          >
            {copied ? <Check /> : <Copy />} {copied ? 'Copied' : 'Copy JSON'}
          </Button>
          <Button variant="outline" size="sm" onClick={() => downloadJson(result, `result-${id}-${result.template_id}-v${result.version}.json`)}>
            <Download /> Download
          </Button>
        </div>
      </div>

      <Card className="grid grid-cols-2 gap-4 p-4 md:grid-cols-5">
        <Readout label="Fields read" value={fields.length} />
        <Readout label="Auto-approved" value={counts.auto} sub={pct(counts.auto, total)} />
        <Readout label="Spot check" value={counts.spot_check} tone="spot" sub={pct(counts.spot_check, total)} />
        <Readout label="Needs review" value={counts.review} tone="review" sub={pct(counts.review, total)} />
        <Readout label="Average confidence" value={fmtConf(mean)} sub="out of 100" />
      </Card>

      <div className={cn('grid gap-4', showJson ? 'xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]' : 'grid-cols-1')}>
        <Card className="overflow-hidden">
          <CardHeader>
            <CardTitle>Extracted fields</CardTitle>
            <Button variant="outline" size="sm" aria-pressed={showJson} onClick={() => setShowJson((v) => !v)}>
              <Braces /> {showJson ? 'Hide JSON' : 'Show JSON'}
            </Button>
          </CardHeader>
          <div className="overflow-x-auto">
            <table className="w-full min-w-xl table-fixed text-left text-sm">
              <caption className="sr-only">Values extracted from document {id}, most in need of attention first</caption>
              <thead>
                <tr className="border-b border-border bg-surface-sunken">
                  <th scope="col" className="label w-1/4 px-4 py-2">Field</th>
                  <th scope="col" className="label w-1/3 px-4 py-2">Value</th>
                  <th scope="col" className="label w-1/5 px-4 py-2">Confidence</th>
                  <th scope="col" className="label px-4 py-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {[...fields]
                  .sort((a, b) => (TIER_RANK[a.approval_tier] ?? 3) - (TIER_RANK[b.approval_tier] ?? 3) || a.field_id.localeCompare(b.field_id))
                  .map((f) => (
                    <FieldRow
                      key={f.field_id}
                      resultId={id}
                      field={f}
                      meta={fieldsMeta[f.field_id]}
                      onSaved={(fresh) => setResult((r) => ({ ...r, ...fresh }))}
                    />
                  ))}
              </tbody>
            </table>
          </div>
        </Card>

        {showJson && (
          <Card className="flex flex-col overflow-hidden">
            <CardHeader>
              <CardTitle>Output JSON</CardTitle>
              <span className="text-xs text-foreground-subtle">Exactly what the API returns</span>
            </CardHeader>
            <pre className="scroll-thin max-h-screen flex-1 overflow-auto bg-surface-sunken p-4 font-mono text-xs">
              <JsonView json={json} />
            </pre>
          </Card>
        )}
      </div>
    </div>
  );
}

function FieldRow({ resultId, field: f, meta, onSaved }) {
  return (
    <tr className={cn('border-b border-border last:border-b-0', f.approval_tier === 'review' && 'bg-danger-surface')}>
      <th scope="row" className="px-4 py-2 text-left align-top font-normal">
        <span className="block break-words font-medium text-foreground">{meta?.label ?? f.field_id}</span>
        {f.reasons.length > 0 && <span className="block text-xs text-danger">{f.reasons.map((r) => REASONS[r] ?? r).join(' · ')}</span>}
      </th>
      <td className="px-4 py-2 align-top">
        <FieldValueCell resultId={resultId} field={f} onSaved={onSaved} />
      </td>
      <td className="px-4 py-2 align-top">
        <ConfidenceGauge value={f.confidence} floor={meta?.confidence_floor} tier={f.approval_tier} size="sm" />
      </td>
      <td className="px-4 py-2 align-top">
        <TierBadge tier={f.approval_tier} size="sm" />
      </td>
    </tr>
  );
}

function downloadJson(data, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/**
 * A field's value, editable in place. The confirmation only appears once the PATCH has
 * actually round-tripped: "saving" while the request is in flight, "saved" only on a real
 * 200 (never optimistic), and a retry affordance - not a silent failure - if it errors.
 * On success it re-fetches the whole result rather than trusting a locally-built guess, so
 * overall_status and everything else the write could affect stay exactly what the server has.
 */
function FieldValueCell({ resultId, field, onSaved }) {
  const [mode, setMode] = useState('view'); // view | editing
  const [value, setValue] = useState(field.value);
  const [state, setState] = useState({ status: 'idle' }); // idle | saving | saved | error
  const inputRef = useRef(null);

  useEffect(() => {
    if (mode === 'editing') inputRef.current?.select();
  }, [mode]);

  async function save(withValue) {
    setState({ status: 'saving' });
    try {
      await correctResultField(resultId, field.field_id, withValue);
      const fresh = await getExtractionResult(resultId);
      onSaved(fresh);
      setMode('view');
      setState({ status: 'saved' });
      setTimeout(() => setState((s) => (s.status === 'saved' ? { status: 'idle' } : s)), 2000);
    } catch (err) {
      setState({ status: 'error', value: withValue, messages: errorLines(err) });
    }
  }

  function cancel() {
    setMode('view');
    setValue(field.value);
  }

  if (mode === 'editing') {
    return (
      <form
        className="flex min-w-0 items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          save(value);
        }}
      >
        <Input
          ref={inputRef}
          mono
          className="h-8"
          aria-label={`Value for ${field.field_id}`}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Escape' && cancel()}
          disabled={state.status === 'saving'}
        />
        <Button type="submit" variant="ghost" size="icon" disabled={state.status === 'saving'} aria-label="Save">
          {state.status === 'saving' ? <Loader2 className="animate-spin" /> : <Check />}
        </Button>
        <Button type="button" variant="ghost" size="icon" disabled={state.status === 'saving'} onClick={cancel} aria-label="Cancel">
          <X />
        </Button>
      </form>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex min-w-0 items-center gap-2">
        <span className="truncate font-mono text-sm text-foreground" title={field.value}>
          {field.value || <span className="font-sans text-foreground-subtle">Empty</span>}
        </span>
        <Button variant="ghost" size="icon" onClick={() => setMode('editing')} aria-label={`Correct ${field.field_id}`}>
          <Pencil />
        </Button>
        {state.status === 'saved' && (
          <span role="status" className="flex shrink-0 items-center gap-1 text-xs text-success">
            <Check className="size-3.5" aria-hidden /> Saved
          </span>
        )}
      </div>
      {state.status === 'error' && (
        <p role="alert" className="flex items-center gap-2 text-xs text-danger">
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
          <span className="truncate">{state.messages.join('; ')}</span>
          <button className="inline-flex shrink-0 items-center gap-1 underline underline-offset-2" onClick={() => save(state.value)}>
            <RotateCcw className="size-3.5" aria-hidden /> Retry
          </button>
        </p>
      )}
    </div>
  );
}

const TOKEN = /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

/** Syntax-coloured JSON, with each approval_tier value in its tier's colour. */
function JsonView({ json }) {
  const out = [];
  let last = 0;
  let prevKey = null;
  let m;
  let i = 0;
  while ((m = TOKEN.exec(json))) {
    if (m.index > last) out.push(json.slice(last, m.index));
    const tok = m[0];
    let cls = 'text-foreground';
    if (tok.startsWith('"') && m[2]) {
      cls = 'text-foreground-secondary';
      prevKey = tok.slice(1, tok.indexOf('"', 1));
    } else if (tok.startsWith('"')) {
      if (prevKey === 'approval_tier') {
        cls = tok === '"review"' ? 'font-medium text-danger' : tok === '"spot_check"' ? 'font-medium text-warning' : 'font-medium text-success';
      } else if (prevKey === 'overall_status' && tok === '"needs_review"') cls = 'font-medium text-danger';
      prevKey = null;
    } else if (tok === 'true' || tok === 'false' || tok === 'null') {
      cls = tok === 'true' && prevKey === 'needs_review' ? 'text-danger' : 'text-foreground-subtle';
      prevKey = null;
    } else {
      cls = 'text-primary-text'; // numbers
      prevKey = null;
    }
    out.push(
      <span key={i++} className={cls}>
        {tok}
      </span>,
    );
    last = m.index + tok.length;
  }
  out.push(json.slice(last));
  return out;
}
