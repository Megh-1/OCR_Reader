import { motion } from 'framer-motion';
import { Braces, Check, Copy, Download, FileSearch } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { getExtractionResult, getTemplateVersion, listResults } from '@/api';
import { EmptyState, PageHeader, Readout, TierBar } from '@/components/bits';
import { ConfidenceGauge } from '@/components/ConfidenceGauge';
import { TierBadge } from '@/components/TierBadge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { navigate } from '@/lib/nav';
import { AUTO_THRESHOLD, REASONS, tierCounts } from '@/lib/tiers';
import { cn, fmtConf, timeAgo } from '@/lib/utils';

const TIER_FILL = { auto: 'var(--chart-1)', spot_check: 'var(--chart-2)', review: 'var(--chart-3)' };
const AXIS = { fill: 'var(--graphite-500)', fontFamily: 'IBM Plex Mono', fontSize: 10 };

export default function Results({ param }) {
  const [list, setList] = useState([]);
  const [listState, setListState] = useState('loading');
  const [detail, setDetail] = useState(null);

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

  return (
    <div className="flex h-full flex-col">
      <PageHeader eyebrow="04 — Results" title="Output & confidence" meta="GET /results  ·  GET /results/{id} — the compiled JSON, per document" />
      <div className="grid min-h-0 flex-1 grid-cols-[300px_minmax(0,1fr)]">
        <div className="scroll-thin min-h-0 overflow-y-auto border-r border-border">
          {listState === 'ready' && list.length === 0 && (
            <EmptyState icon={FileSearch} title="No results yet">
              Process a batch and every confirmed page lands here.
            </EmptyState>
          )}
          {list.map((r) => (
            <button
              key={r.id}
              onClick={() => navigate('results', r.id)}
              className={cn(
                'flex w-full flex-col gap-2 border-b border-border px-4 py-3 text-left transition-colors',
                r.id === selectedId ? 'bg-accent' : 'hover:bg-muted',
              )}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-[12px] text-faint-foreground">#{r.id}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">
                  {r.template_id}
                  <span className="text-faint-foreground">@v{r.version}</span>
                </span>
                {r.overall_status === 'needs_review' ? (
                  <span className="size-2 rounded-[1px] bg-tier-review" title="needs review" />
                ) : (
                  <span className="size-2 rounded-[1px] bg-tier-auto" title="complete" />
                )}
              </div>
              <TierBar counts={r.tier_counts} animated={false} />
              <div className="flex justify-between font-mono text-[10.5px] text-faint-foreground">
                <span>
                  {r.tier_counts.auto} · <span className="text-tier-spot-foreground">{r.tier_counts.spot_check}</span> ·{' '}
                  <span className="text-tier-review-text">{r.tier_counts.review}</span>
                </span>
                <span>{timeAgo(r.extracted_at)}</span>
              </div>
            </button>
          ))}
        </div>

        <div className="scroll-thin min-h-0 overflow-y-auto">
          {detail?.result && detail.id === selectedId ? (
            <ResultDetail detail={detail} history={list.filter((r) => r.template_id === detail.result.template_id).slice(0, 14)} />
          ) : detail?.error ? (
            <EmptyState title="Couldn't load this result" />
          ) : (
            listState !== 'ready' || list.length ? <div className="p-6 text-[13px] text-muted-foreground">Loading…</div> : null
          )}
        </div>
      </div>
    </div>
  );
}

function ResultDetail({ detail, history }) {
  const { result, template, id } = detail;
  const fieldsMeta = useMemo(() => Object.fromEntries((template?.fields ?? []).map((f) => [f.id, f])), [template]);
  const fields = Object.values(result.fields);
  const counts = tierCounts(result.fields);
  const n = fields.length || 1;
  const confs = fields.map((f) => f.confidence);
  const mean = confs.reduce((a, b) => a + b, 0) / n;
  const weakest = [...fields].sort((a, b) => a.confidence - b.confidence)[0];
  const [copied, setCopied] = useState(false);
  const json = JSON.stringify(result, null, 2);

  const perField = [...fields]
    .sort((a, b) => a.confidence - b.confidence)
    .map((f) => ({ id: f.field_id, confidence: f.confidence, tier: f.approval_tier, floor: (fieldsMeta[f.field_id]?.confidence_floor ?? 0) * 100 }));
  const floors = [...new Set(perField.map((f) => f.floor).filter(Boolean))];
  const trend = [...history].reverse().map((r) => ({ id: r.id, name: `#${r.id}`, ...r.tier_counts, current: r.id === id }));

  function download() {
    const blob = new Blob([json], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `result-${id}-${result.template_id}-v${result.version}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <div className="flex flex-col gap-5 p-6">
      <div className="flex items-center gap-3">
        <span className="font-mono text-[20px] font-semibold">#{id}</span>
        <span className="font-mono text-[14px] text-secondary-foreground">
          {result.template_id}
          <span className="text-faint-foreground">@v{result.version}</span>
        </span>
        <span
          className={cn(
            'caps rounded-sm border px-2 py-0.5',
            result.overall_status === 'needs_review' ? 'border-tier-review text-tier-review-text' : 'border-border-strong text-muted-foreground',
          )}
        >
          {result.overall_status.replace('_', ' ')}
        </span>
        <span className="font-mono text-[12px] text-faint-foreground">{new Date(result.extracted_at).toLocaleString()}</span>
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
          <Button variant="outline" size="sm" onClick={download}>
            <Download /> Download
          </Button>
        </div>
      </div>

      {/* ---------------------------------------------------------------- readouts */}
      <Card className="grid grid-cols-6 gap-4 p-4">
        <Readout label="Fields" value={fields.length} />
        <Readout label="Auto" value={counts.auto} sub={`${Math.round((100 * counts.auto) / n)}% · no person needed`} />
        <Readout label="Spot check" value={counts.spot_check} tone="spot" sub={`${Math.round((100 * counts.spot_check) / n)}% · a glance`} />
        <Readout label="Review" value={counts.review} tone="review" sub={`${Math.round((100 * counts.review) / n)}% · must act`} />
        <Readout label="Mean confidence" value={fmtConf(mean)} sub="tesseract, 0–100" />
        <Readout label="Weakest read" value={fmtConf(weakest?.confidence)} sub={weakest?.field_id} tone={weakest?.approval_tier === 'review' ? 'review' : 'default'} />
      </Card>

      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)] gap-5">
        <Card>
          <CardHeader>
            <CardTitle>Approval split · {result.template_id}, recent documents</CardTitle>
            <Legend />
          </CardHeader>
          <div className="h-[300px] px-2 pt-3 pb-1">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={trend} margin={{ top: 8, right: 12, bottom: 0, left: -18 }} barCategoryGap="22%">
                <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
                <XAxis dataKey="name" tick={AXIS} axisLine={{ stroke: 'var(--graphite-700)' }} tickLine={false} />
                <YAxis tick={AXIS} axisLine={false} tickLine={false} allowDecimals={false} />
                <Tooltip content={<SplitTooltip />} cursor={{ fill: 'var(--graphite-850)' }} />
                {['auto', 'spot_check', 'review'].map((k) => (
                  <Bar key={k} dataKey={k} stackId="t" fill={TIER_FILL[k]} isAnimationActive animationDuration={700}>
                    {trend.map((d) => (
                      <Cell key={d.id} fillOpacity={d.current ? 1 : 0.38} stroke={d.current ? 'var(--graphite-100)' : 'none'} />
                    ))}
                  </Bar>
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="px-4 pb-3 text-[12px] text-muted-foreground">
            Bright bar = this document. Grey is work no one has to do; every orange field is a person's time.
          </div>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Per-field confidence · weakest first</CardTitle>
            <span className="font-mono text-[11px] text-faint-foreground">
              {floors.map((f) => `floor ${f}`).join(' · ')} · auto {AUTO_THRESHOLD}
            </span>
          </CardHeader>
          <div className="px-2 pt-2" style={{ height: Math.max(260, perField.length * 17 + 40) }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={perField} layout="vertical" margin={{ top: 4, right: 36, bottom: 4, left: 8 }} barCategoryGap={3}>
                <CartesianGrid horizontal={false} stroke="var(--chart-grid)" />
                <XAxis type="number" domain={[0, 100]} ticks={[0, 20, 40, 60, 80, 100]} tick={AXIS} axisLine={false} tickLine={false} />
                <YAxis type="category" dataKey="id" width={168} tick={{ ...AXIS, fill: 'var(--graphite-300)' }} axisLine={false} tickLine={false} interval={0} />
                <Tooltip content={<FieldTooltip />} cursor={{ fill: 'var(--graphite-850)' }} />
                {floors.map((f) => (
                  <ReferenceLine key={f} x={f} stroke="var(--graphite-300)" strokeDasharray="3 3" />
                ))}
                <ReferenceLine x={AUTO_THRESHOLD} stroke="var(--graphite-100)" strokeWidth={1.5} label={{ value: 'auto', fill: 'var(--graphite-100)', fontSize: 10, position: 'top', fontFamily: 'IBM Plex Mono' }} />
                <Bar dataKey="confidence" radius={[0, 1, 1, 0]} isAnimationActive animationDuration={800}>
                  {perField.map((d) => (
                    <Cell key={d.id} fill={TIER_FILL[d.tier] ?? 'var(--graphite-600)'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)] gap-5">
        <Card className="overflow-hidden">
          <CardHeader>
            <CardTitle>Fields</CardTitle>
            <span className="font-mono text-[11px] text-faint-foreground">{fields.length} extracted</span>
          </CardHeader>
          <div className="grid grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)_150px_104px] gap-3 border-b border-border px-4 h-8 items-center caps-sm text-faint-foreground">
            <span>Field</span>
            <span>Value</span>
            <span>Confidence</span>
            <span>Tier</span>
          </div>
          {[...fields]
            .sort((a, b) => order(a) - order(b) || a.field_id.localeCompare(b.field_id))
            .map((f, i) => (
              <motion.div
                key={f.field_id}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(i * 0.015, 0.4) }}
                className={cn(
                  'grid grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)_150px_104px] items-center gap-3 border-b border-border px-4 py-2 last:border-b-0',
                  f.approval_tier === 'review' && 'bg-tier-review-surface/40',
                )}
              >
                <div className="min-w-0">
                  <div className="truncate font-mono text-[12px] text-secondary-foreground">{f.field_id}</div>
                  {f.reasons.length > 0 && (
                    <div className="truncate text-[11px] text-tier-review-text">{f.reasons.map((r) => REASONS[r] ?? r).join(' · ')}</div>
                  )}
                </div>
                <span className={cn('truncate font-mono text-[12.5px]', f.approval_tier === 'review' && 'text-signal-300')} title={f.value}>
                  {f.value || <span className="text-faint-foreground">∅</span>}
                </span>
                <ConfidenceGauge value={f.confidence} floor={fieldsMeta[f.field_id]?.confidence_floor} tier={f.approval_tier} size="sm" />
                <TierBadge tier={f.approval_tier} size="sm" />
              </motion.div>
            ))}
        </Card>

        <Card className="flex flex-col overflow-hidden">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Braces className="size-3.5" /> Output JSON
            </CardTitle>
            <span className="font-mono text-[11px] text-faint-foreground">exactly what the API returns</span>
          </CardHeader>
          <pre className="scroll-thin max-h-[760px] flex-1 overflow-auto bg-well p-4 font-mono text-[11.5px] leading-[18px] shadow-[inset_0_1px_2px_rgba(0,0,0,0.6)]">
            <JsonView json={json} />
          </pre>
        </Card>
      </div>
    </div>
  );
}

const order = (f) => ({ review: 0, spot_check: 1, auto: 2 })[f.approval_tier] ?? 3;

function Legend() {
  return (
    <div className="flex items-center gap-3 font-mono text-[10.5px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span className="size-2 rounded-[1px] bg-chart-1" /> auto
      </span>
      <span className="flex items-center gap-1.5">
        <span className="size-2 rounded-[1px] bg-chart-2" /> spot
      </span>
      <span className="flex items-center gap-1.5">
        <span className="size-2 rounded-[1px] bg-chart-3" /> review
      </span>
    </div>
  );
}

function TooltipBox({ children }) {
  return (
    <div className="rounded-md border border-border-strong bg-popover px-3 py-2 font-mono text-[11px] shadow-[0_12px_32px_-4px_rgba(0,0,0,0.55)]">
      {children}
    </div>
  );
}

function SplitTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <TooltipBox>
      <div className="mb-1 text-foreground">result {d.name}</div>
      <div className="text-muted-foreground">auto {d.auto}</div>
      <div className="text-tier-spot-foreground">spot check {d.spot_check}</div>
      <div className="text-tier-review-text">review {d.review}</div>
    </TooltipBox>
  );
}

function FieldTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <TooltipBox>
      <div className="mb-1 text-foreground">{d.id}</div>
      <div className="text-secondary-foreground">
        confidence {fmtConf(d.confidence)} · floor {d.floor} · {d.tier}
      </div>
    </TooltipBox>
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
    let cls = 'text-graphite-200';
    if (tok.startsWith('"') && m[2]) {
      cls = 'text-graphite-500';
      prevKey = tok.slice(1, tok.indexOf('"', 1));
    } else if (tok.startsWith('"')) {
      if (prevKey === 'approval_tier') {
        cls = tok === '"review"' ? 'text-tier-review-foreground bg-tier-review px-0.5' : tok === '"spot_check"' ? 'text-tier-spot-foreground' : 'text-graphite-300';
      } else if (prevKey === 'overall_status' && tok === '"needs_review"') cls = 'text-tier-review-text';
      else cls = 'text-graphite-100';
      prevKey = null;
    } else if (tok === 'true' || tok === 'false' || tok === 'null') {
      cls = tok === 'true' && prevKey === 'needs_review' ? 'text-tier-review-text' : 'text-graphite-400';
      prevKey = null;
    } else {
      cls = 'text-graphite-300'; // numbers
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
