import { FabricText, Rect } from 'fabric';
import {
  Crosshair,
  FileImage,
  GitCommitHorizontal,
  Lock,
  MousePointer2,
  Save,
  Square,
  SquareDashed,
  Trash2,
  Upload,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createTemplate,
  errorLines,
  getTemplate,
  getTemplateVersion,
  listResults,
  listTemplates,
  listTemplateVersions,
  resultImageUrl,
} from '@/api';
import { EmptyState, PageHeader } from '@/components/bits';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Kbd } from '@/components/ui/card';
import { Input, Label, NativeSelect } from '@/components/ui/input';
import { rectToRelativeBBox, relativeBBoxToRect } from '@/geometry';
import { useDocumentCanvas } from '@/useDocumentCanvas';
import { cn, timeAgo } from '@/lib/utils';

const MIN_BOX_SIZE = 8; // px in canvas space; drags smaller than this are accidental clicks

// Layout the canvas is sized against (see the grid in the component below).
const PANEL_W = 416;
const BED_PADDING = 48;
const SIDEBAR_W = { narrow: 64, wide: 240 };
const CHROME_H = 96 + 56 + 48 + BED_PADDING; // page header + tool bar + status bar + padding

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const withAlpha = (hex, alpha) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
};
// Template boxes are drawn in the brand color: blue means "this is something you can edit".
const fieldStyle = () => ({ stroke: cssVar('--primary'), fill: withAlpha(cssVar('--primary'), 0.12), strokeWidth: 2, strokeUniform: true });
const anchorStyle = () => ({ stroke: cssVar('--primary'), fill: withAlpha(cssVar('--primary'), 0.04), strokeWidth: 2, strokeDashArray: [8, 4], strokeUniform: true });
const controlStyle = () => ({
  borderColor: cssVar('--primary'),
  cornerColor: cssVar('--surface'),
  cornerStrokeColor: cssVar('--primary'),
  transparentCorners: false,
  cornerSize: 8,
  cornerStyle: 'rect',
  borderScaleFactor: 2,
});

let nextFieldSeq = 1;
function newBox(id, type) {
  if (type === 'anchor') return { id, type: 'anchor', text: '', matchThreshold: 0.7 };
  return { id, type: 'field', fieldId: `field_${nextFieldSeq++}`, label: '', dataType: 'string', validationRegex: '', confidenceFloor: 0.6, checksum: null };
}

const LABEL_MAX = 24; // characters; longer anchor text is cut so the tab stays inside the page
const shorten = (text) => (text.length > LABEL_MAX ? `${text.slice(0, LABEL_MAX - 1)}…` : text);

const fmtBox = (b) => (b ? b.map((n) => n.toFixed(4)).join('  ') : '—');

export default function TemplateDesigner() {
  const [size] = useState(() => ({
    w: Math.max(560, Math.min(1100, window.innerWidth - (window.innerWidth >= 1024 ? SIDEBAR_W.wide : SIDEBAR_W.narrow) - PANEL_W - BED_PADDING)),
    h: Math.max(520, window.innerHeight - CHROME_H),
  }));
  // No default sample document - an empty canvas until the user uploads one or opens an
  // existing template with a processed page to draw over.
  const [imageUrl, setImageUrl] = useState(null);
  const { canvasElRef, fabricCanvasRef, imageRef, imageReady } = useDocumentCanvas(imageUrl, size.w, size.h);

  const rectsRef = useRef(new Map()); // boxId -> fabric Rect
  const labelsRef = useRef(new Map()); // boxId -> fabric Text
  const modeRef = useRef(null);
  const cursorRef = useRef(null);
  const pendingLoadRef = useRef(null); // a template waiting for its page image to finish loading
  const hintsRef = useRef(null);
  // Not editable here yet, but they must survive a load-and-save like `checksum` and `hints` do:
  // every version is rebuilt from this state, so anything not carried is silently dropped.
  const [checks, setChecks] = useState([]);
  const uploadedUrlRef = useRef(null);

  const [mode, setModeState] = useState(null); // null | 'field' | 'anchor'
  const [boxes, setBoxes] = useState([]);
  const [hintOnly, setHintOnly] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const [readout, setReadout] = useState(null);
  const [templateId, setTemplateId] = useState('');
  const [loaded, setLoaded] = useState(null); // { template_id, version }
  const [templates, setTemplates] = useState([]);
  const [results, setResults] = useState([]);
  const [versions, setVersions] = useState([]);
  const [tab, setTab] = useState('fields');
  const [save, setSave] = useState({ status: 'idle' });

  useEffect(() => {
    listTemplates().then((t) => setTemplates(t.sort())).catch(() => {});
    listResults(80).then(setResults).catch(() => {});
  }, []);

  const refreshVersions = useCallback((id) => {
    if (!id) return setVersions([]);
    listTemplateVersions(id).then(setVersions).catch(() => setVersions([]));
  }, []);

  const setMode = useCallback(
    (m) => {
      // The ref changes in the same tick as the click: a fast click-then-drag must not see a stale mode.
      modeRef.current = m;
      setModeState(m);
      const canvas = fabricCanvasRef.current;
      if (canvas) {
        canvas.selection = !m;
        canvas.skipTargetFind = !!m;
        canvas.defaultCursor = m ? 'crosshair' : 'default';
        if (m) canvas.discardActiveObject();
        canvas.requestRenderAll();
      }
    },
    [fabricCanvasRef],
  );

  const labelFor = useCallback((box) => (box.type === 'anchor' ? `⌖ ${shorten(box.text || 'anchor')}` : box.fieldId), []);

  const syncLabel = useCallback((boxId) => {
    const rect = rectsRef.current.get(boxId);
    const label = labelsRef.current.get(boxId);
    if (!rect || !label) return;
    label.set({ left: rect.left - 1, top: rect.top - label.height - 1 });
  }, []);

  const addRect = useCallback(
    (boxId, rect, box) => {
      const canvas = fabricCanvasRef.current;
      rect.set(controlStyle());
      rectsRef.current.set(boxId, rect);
      const label = new FabricText(labelFor(box), {
        fontFamily: 'IBM Plex Mono',
        fontSize: 12,
        fill: cssVar('--primary-foreground'),
        backgroundColor: cssVar('--primary'),
        padding: 2,
        selectable: false,
        evented: false,
      });
      labelsRef.current.set(boxId, label);
      canvas.add(label);
      syncLabel(boxId);
    },
    [fabricCanvasRef, labelFor, syncLabel],
  );

  // A box's bbox is relative to the image it was drawn on - never carried onto another image.
  function switchImage(url) {
    if (uploadedUrlRef.current && url !== uploadedUrlRef.current) URL.revokeObjectURL(uploadedUrlRef.current);
    rectsRef.current = new Map();
    labelsRef.current = new Map();
    setBoxes([]);
    setChecks([]); // they name fields of the template that was on the old image
    setSelectedId(null);
    setImageUrl(url);
  }

  const placeTemplate = useCallback(
    (template) => {
      const canvas = fabricCanvasRef.current;
      const image = imageRef.current;
      if (!canvas || !image) return;
      for (const r of rectsRef.current.values()) canvas.remove(r);
      for (const l of labelsRef.current.values()) canvas.remove(l);
      rectsRef.current = new Map();
      labelsRef.current = new Map();
      const next = [];
      const place = (bbox, style, box) => {
        const rect = new Rect({ ...relativeBBoxToRect(bbox, image), originX: 'left', originY: 'top', ...style });
        canvas.add(rect);
        addRect(box.id, rect, box);
        next.push(box);
      };
      for (const a of template.anchors) {
        place(a.bbox, anchorStyle(), { id: crypto.randomUUID(), type: 'anchor', text: a.text, matchThreshold: a.match_threshold });
      }
      for (const f of template.fields) {
        place(f.bbox, fieldStyle(), {
          id: crypto.randomUUID(),
          type: 'field',
          fieldId: f.id,
          label: f.label,
          dataType: f.data_type,
          validationRegex: f.validation_regex ?? '',
          confidenceFloor: f.confidence_floor,
          // Not editable here, but it must survive a load-and-save: every field is rebuilt from
          // this state, so anything not carried is silently dropped from the new version.
          checksum: f.checksum ?? null,
        });
      }
      canvas.requestRenderAll();
      hintsRef.current = template.hints ?? null;
      setChecks(template.checks ?? []);
      setHintOnly(Boolean(template.hints?.hint_only));
      setBoxes(next);
      setSelectedId(null);
      setTemplateId(template.template_id);
      setLoaded({ template_id: template.template_id, version: template.version });
      setSave({ status: 'idle' });
      refreshVersions(template.template_id);
    },
    [fabricCanvasRef, imageRef, addRect, refreshVersions],
  );

  // A template picked before its page finished loading is placed as soon as the image is ready.
  useEffect(() => {
    if (imageReady && pendingLoadRef.current) {
      const t = pendingLoadRef.current;
      pendingLoadRef.current = null;
      placeTemplate(t);
    }
  }, [imageReady, placeTemplate]);

  async function loadTemplate(id, version) {
    if (!id) return;
    try {
      const template = version ? await getTemplateVersion(id, version) : await getTemplate(id);
      // Draw it over a real processed page of that type when one exists. With no document
      // loaded and no such page to fall back to, there's nothing to draw boxes over yet -
      // ask for one rather than silently doing nothing (or, as before, defaulting to an
      // unrelated placeholder image).
      const page = results.find((r) => r.template_id === id && r.has_image);
      const url = page ? resultImageUrl(page.id) : imageUrl;
      if (!url) {
        setSave({ status: 'error', messages: [`No processed document found for ${id} yet - upload one or pick "Document from..." first.`] });
        return;
      }
      if (url !== imageUrl) {
        pendingLoadRef.current = template;
        switchImage(url);
      } else {
        placeTemplate(template);
      }
    } catch (err) {
      setSave({ status: 'error', messages: errorLines(err) });
    }
  }

  // Canvas interaction. Re-bound whenever the image changes, because useDocumentCanvas builds a
  // fresh Fabric canvas for every image - handlers bound once would stay on the disposed one.
  useEffect(() => {
    const canvas = fabricCanvasRef.current;
    if (!canvas) return undefined;
    let drawing = null;
    let start = null;
    const idOf = (obj) => [...rectsRef.current.entries()].find(([, r]) => r === obj)?.[0] ?? null;
    const bboxOf = (rect) => (imageRef.current ? rectToRelativeBBox(rect, imageRef.current) : null);

    function down(opt) {
      const m = modeRef.current;
      if (!m) return;
      start = canvas.getScenePoint(opt.e);
      drawing = new Rect({ left: start.x, top: start.y, width: 0, height: 0, originX: 'left', originY: 'top', ...(m === 'anchor' ? anchorStyle() : fieldStyle()) });
      canvas.add(drawing);
    }
    function move(opt) {
      const p = canvas.getScenePoint(opt.e);
      const img = imageRef.current;
      if (img && cursorRef.current) {
        const x = (p.x - img.left) / img.getScaledWidth();
        const y = (p.y - img.top) / img.getScaledHeight();
        cursorRef.current.textContent = x >= 0 && x <= 1 && y >= 0 && y <= 1 ? `${x.toFixed(4)}, ${y.toFixed(4)}` : '—';
      }
      if (!drawing) return;
      drawing.set({ left: Math.min(p.x, start.x), top: Math.min(p.y, start.y), width: Math.abs(p.x - start.x), height: Math.abs(p.y - start.y) });
      setReadout({ kind: modeRef.current, bbox: bboxOf(drawing), live: true });
      canvas.requestRenderAll();
    }
    function up() {
      const rect = drawing;
      drawing = null;
      start = null;
      if (!rect) return;
      if (rect.width < MIN_BOX_SIZE || rect.height < MIN_BOX_SIZE) {
        canvas.remove(rect);
        canvas.requestRenderAll();
        setReadout(null);
        return;
      }
      const box = newBox(crypto.randomUUID(), modeRef.current);
      addRect(box.id, rect, box);
      setBoxes((prev) => [...prev, box]);
      setMode(null);
      canvas.setActiveObject(rect);
      setSelectedId(box.id);
      setReadout({ kind: box.type, bbox: bboxOf(rect) });
      canvas.requestRenderAll();
    }
    function transformed(e) {
      const id = idOf(e.target);
      if (!id) return;
      syncLabel(id);
      setReadout({ kind: 'box', bbox: bboxOf(e.target), live: true });
    }
    function selected(e) {
      const id = idOf(e.selected?.[0]);
      setSelectedId(id);
      if (id) setReadout({ kind: 'box', bbox: bboxOf(rectsRef.current.get(id)) });
    }
    canvas.on('mouse:down', down);
    canvas.on('mouse:move', move);
    canvas.on('mouse:up', up);
    canvas.on('object:moving', transformed);
    canvas.on('object:scaling', transformed);
    canvas.on('selection:created', selected);
    canvas.on('selection:updated', selected);
    canvas.on('selection:cleared', () => setSelectedId(null));
    return () => {
      canvas.off();
    };
  }, [imageUrl, fabricCanvasRef, imageRef, addRect, syncLabel, setMode]);

  // Keep the on-canvas id tabs in step with the list.
  useEffect(() => {
    const canvas = fabricCanvasRef.current;
    if (!canvas) return;
    for (const b of boxes) {
      const label = labelsRef.current.get(b.id);
      if (label && label.text !== labelFor(b)) {
        label.set({ text: labelFor(b) });
        syncLabel(b.id);
      }
    }
    canvas.requestRenderAll();
  }, [boxes, fabricCanvasRef, labelFor, syncLabel]);

  const selectBox = useCallback(
    (id) => {
      setSelectedId(id);
      const canvas = fabricCanvasRef.current;
      const rect = rectsRef.current.get(id);
      if (canvas && rect) {
        setMode(null);
        canvas.setActiveObject(rect);
        canvas.requestRenderAll();
        if (imageRef.current) setReadout({ kind: 'box', bbox: rectToRelativeBBox(rect, imageRef.current) });
      }
    },
    [fabricCanvasRef, imageRef, setMode],
  );

  const deleteBox = useCallback(
    (id) => {
      const canvas = fabricCanvasRef.current;
      const rect = rectsRef.current.get(id);
      const label = labelsRef.current.get(id);
      if (canvas) {
        if (rect) canvas.remove(rect);
        if (label) canvas.remove(label);
        canvas.discardActiveObject();
        canvas.requestRenderAll();
      }
      rectsRef.current.delete(id);
      labelsRef.current.delete(id);
      setBoxes((prev) => prev.filter((b) => b.id !== id));
      setSelectedId(null);
    },
    [fabricCanvasRef],
  );

  useEffect(() => {
    function onKey(e) {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement || e.target instanceof HTMLTextAreaElement;
      if (typing || e.metaKey || e.ctrlKey) return;
      if (e.key === 'v' || e.key === 'Escape') setMode(null);
      else if (e.key === 'f') setMode('field');
      else if (e.key === 'a') setMode('anchor');
      else if ((e.key === 'Delete' || e.key === 'Backspace') && selectedId) deleteBox(selectedId);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setMode, selectedId, deleteBox]);

  const update = (id, patch) => setBoxes((prev) => prev.map((b) => (b.id === id ? { ...b, ...patch } : b)));

  const anchors = boxes.filter((b) => b.type === 'anchor');
  const fields = boxes.filter((b) => b.type === 'field');
  const latest = versions[0]?.version ?? loaded?.version ?? 0;
  const nextVersion = templateId && templateId === loaded?.template_id ? latest + 1 : versions.length ? latest + 1 : 1;
  const blankAnchors = anchors.filter((b) => !b.text.trim()).length;
  const canSave = imageReady && anchors.length >= 1 && blankAnchors === 0 && fields.length >= 1 && templateId.trim().length > 0;

  async function handleSave() {
    const image = imageRef.current;
    const hints = hintsRef.current || hintOnly ? { ...(hintsRef.current ?? {}), hint_only: hintOnly } : null;
    const payload = { template_id: templateId.trim(), anchors: [], fields: [], checks, ...(hints ? { hints } : {}) };
    for (const b of boxes) {
      const rect = rectsRef.current.get(b.id);
      if (!rect) continue;
      const bbox = rectToRelativeBBox(rect, image);
      if (b.type === 'anchor') payload.anchors.push({ text: b.text, bbox, match_threshold: Number(b.matchThreshold) });
      else
        payload.fields.push({
          id: b.fieldId,
          label: b.label,
          bbox,
          data_type: b.dataType,
          validation_regex: b.validationRegex || null,
          confidence_floor: Number(b.confidenceFloor),
          checksum: b.checksum ?? null,
        });
    }
    setSave({ status: 'saving' });
    try {
      const out = await createTemplate(payload);
      setSave({ status: 'saved', result: out });
      setLoaded({ template_id: out.template_id, version: out.version });
      refreshVersions(out.template_id);
      if (!templates.includes(out.template_id)) setTemplates((t) => [...t, out.template_id].sort());
    } catch (err) {
      setSave({ status: 'error', messages: errorLines(err) });
    }
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={loaded ? loaded.template_id : 'New template'}
        meta={
          loaded
            ? `Editing version ${loaded.version}. Saving creates version ${nextVersion}; earlier versions are never changed.`
            : 'Draw a box around each value you want to extract, on a sample document, then save.'
        }
      >
        <NativeSelect className="w-52" aria-label="Open a saved template" value="" onChange={(e) => loadTemplate(e.target.value)}>
          <option value="">Open template…</option>
          {templates.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect
          className="w-56"
          aria-label="Use a document you already processed"
          value=""
          onChange={(e) => {
            const v = e.target.value;
            if (v) switchImage(resultImageUrl(v));
          }}
        >
          <option value="">Use a processed document…</option>
          {results
            .filter((r) => r.has_image)
            .slice(0, 30)
            .map((r) => (
              <option key={r.id} value={r.id}>
                #{r.id} · {r.template_id} · {timeAgo(r.extracted_at)}
              </option>
            ))}
        </NativeSelect>
        <Button variant="outline" asChild>
          <label className="cursor-pointer focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-ring">
            <Upload /> Upload image
            <input
              type="file"
              accept="image/*"
              className="sr-only"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                const url = URL.createObjectURL(file);
                uploadedUrlRef.current = url;
                hintsRef.current = null;
                switchImage(url);
                e.target.value = '';
              }}
            />
          </label>
        </Button>
      </PageHeader>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_416px]">
        <section aria-label="Document" className="flex min-h-0 flex-col">
          <div className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-surface px-4 py-2">
            <div role="group" aria-label="Drawing tool" className="flex gap-1 rounded-md border border-border-strong p-1">
              {TOOLS.map(([m, label, Icon, key]) => (
                <button
                  key={label}
                  aria-pressed={mode === m}
                  onClick={() => setMode(m)}
                  className={cn(
                    'flex h-8 items-center gap-2 rounded-sm px-3 text-sm font-medium transition-colors',
                    mode === m ? 'bg-primary text-primary-foreground' : 'text-foreground-secondary hover:bg-surface-sunken hover:text-foreground',
                  )}
                >
                  <Icon className="size-4" aria-hidden />
                  {label}
                  <Kbd className={cn(mode === m && 'border-primary-foreground bg-transparent text-primary-foreground')}>{key}</Kbd>
                </button>
              ))}
            </div>
            {mode && (
              <p className="flex items-center gap-2 text-sm text-foreground-secondary" role="status">
                <Crosshair className="size-4" aria-hidden />
                {mode === 'anchor' ? 'Drag over a printed label that is identical on every document of this type' : 'Drag over a value to extract'}
              </p>
            )}
            <p className="ml-auto flex items-center gap-2 text-xs text-foreground-secondary">
              Position <span ref={cursorRef} className="w-32 font-mono text-foreground">—</span>
            </p>
          </div>

          <div className="relative min-h-0 flex-1 overflow-auto bg-surface-sunken p-6">
            {imageUrl ? (
              <div className="mx-auto w-fit shadow-md">
                <canvas ref={canvasElRef} />
              </div>
            ) : (
              <EmptyState icon={FileImage} title="No document yet" className="h-full">
                Upload an image of a sample document, or pick one you already processed, then draw boxes on it. PDFs can't be drawn on directly: process the PDF
                first, then choose it from the list.
              </EmptyState>
            )}
          </div>

          <div className="flex h-12 items-center gap-6 border-t border-border bg-surface px-4 text-xs text-foreground-secondary">
            <span className="font-medium">{readout?.live ? 'Measuring' : 'Selected box'}</span>
            <span className="font-mono text-foreground">{fmtBox(readout?.bbox)}</span>
            {readout?.bbox && (
              <span>
                w {(readout.bbox[2] - readout.bbox[0]).toFixed(4)} · h {(readout.bbox[3] - readout.bbox[1]).toFixed(4)}
              </span>
            )}
            <span className="ml-auto text-foreground-subtle">
              {anchors.length} anchor{anchors.length === 1 ? '' : 's'} · {fields.length} field{fields.length === 1 ? '' : 's'}
            </span>
          </div>
        </section>

        <aside aria-label="Template settings" className="flex min-h-0 flex-col border-l border-border bg-surface">
          <div className="flex flex-col gap-3 border-b border-border p-4">
            <div className="flex items-end gap-2">
              <Label className="flex-1">
                Template name
                <Input mono value={templateId} onChange={(e) => setTemplateId(e.target.value)} placeholder="e.g. air_waybill" />
              </Label>
              <Button variant="primary" disabled={!canSave || save.status === 'saving'} onClick={handleSave}>
                <Save /> {save.status === 'saving' ? 'Saving…' : `Save v${nextVersion}`}
              </Button>
            </div>
            <label className="flex cursor-pointer items-start gap-2 text-xs text-foreground-secondary">
              <input type="checkbox" className="mt-1 size-4 accent-primary" checked={hintOnly} onChange={(e) => setHintOnly(e.target.checked)} />
              <span>
                <span className="font-medium text-foreground">Only use when I choose it.</span> Tick this for a template that reads part of a layout another template already covers, so the
                two don't clash during automatic detection.
              </span>
            </label>
            {save.status === 'saved' && (
              <p role="status" className="flex items-center gap-2 rounded-md border border-success-border bg-success-surface px-3 py-2 text-sm text-success">
                <GitCommitHorizontal className="size-4 shrink-0" aria-hidden />
                Saved {save.result.template_id} v{save.result.version}
              </p>
            )}
            {save.status === 'error' && (
              <div role="alert" className="rounded-md border border-danger-border bg-danger-surface px-3 py-2 text-sm text-danger">
                {save.messages.map((m) => (
                  <p key={m}>{m}</p>
                ))}
              </div>
            )}
            {!canSave && save.status !== 'error' && save.status !== 'saved' && (
              <p className="text-xs text-foreground-secondary">
                {anchors.length < 1 ? 'Add at least one anchor. ' : ''}
                {blankAnchors > 0 ? 'Every anchor needs its printed text (Anchors tab). ' : ''}
                {fields.length < 1 ? 'Add at least one field. ' : ''}
                {!templateId.trim() ? 'Give the template a name.' : ''}
              </p>
            )}
          </div>

          <div role="tablist" aria-label="Template contents" className="flex border-b border-border px-2">
            {[
              ['fields', `Fields (${fields.length})`],
              ['anchors', `Anchors (${anchors.length})`],
              ['checks', `Checks (${checks.length})`],
              ['versions', `Versions${versions.length ? ` (${versions.length})` : ''}`],
            ].map(([key, label]) => (
              <button
                key={key}
                role="tab"
                aria-selected={tab === key}
                onClick={() => setTab(key)}
                className={cn(
                  'h-10 border-b-2 px-3 text-sm font-medium transition-colors',
                  tab === key ? 'border-primary text-primary-text' : 'border-transparent text-foreground-secondary hover:text-foreground',
                )}
              >
                {label}
              </button>
            ))}
          </div>

          <div role="tabpanel" className="scroll-thin min-h-0 flex-1 overflow-y-auto">
            {tab === 'fields' &&
              (fields.length === 0 ? (
                <PanelHint>Choose the Field tool (F) and drag over a value on the document. Each box becomes one extracted field.</PanelHint>
              ) : (
                fields.map((b) => (
                  <FieldRow key={b.id} box={b} selected={b.id === selectedId} onSelect={() => selectBox(b.id)} onChange={(p) => update(b.id, p)} onDelete={() => deleteBox(b.id)} />
                ))
              ))}
            {tab === 'anchors' &&
              (anchors.length === 0 ? (
                <PanelHint>
                  Choose the Anchor tool (A) and drag over a printed label that is the same on every document of this type. Before reading a document, the system checks that every
                  anchor matches.
                </PanelHint>
              ) : (
                anchors.map((b) => (
                  <AnchorRow key={b.id} box={b} selected={b.id === selectedId} onSelect={() => selectBox(b.id)} onChange={(p) => update(b.id, p)} onDelete={() => deleteBox(b.id)} />
                ))
              ))}
            {tab === 'checks' &&
              (checks.length === 0 ? (
                <PanelHint>
                  Checks compare fields with each other, for example "total = weight x rate". A value that fails one is sent to Review. They are kept when you save, and are added
                  through the API for now.
                </PanelHint>
              ) : (
                <ul>
                  {checks.map((c) => (
                    <li key={c.id} className="border-b border-border px-4 py-3">
                      <p className="text-sm font-medium text-foreground">{c.label}</p>
                      <p className="text-xs text-foreground-secondary">
                        {c.kind.replace('_', ' ')} · {[...c.inputs, c.target].filter(Boolean).join(', ')}
                        {c.when ? ` · only when ${c.when.field} is ${c.when.equals}` : ''}
                      </p>
                    </li>
                  ))}
                </ul>
              ))}
            {tab === 'versions' &&
              (versions.length === 0 ? (
                <PanelHint>Open or save a template to see its history.</PanelHint>
              ) : (
                <div className="flex flex-col">
                  <ul>
                    {versions.map((v, i) => (
                      <li key={v.version} className="flex items-center gap-3 border-b border-border px-4 py-3">
                        <div className="min-w-0 flex-1">
                          <p className="flex items-center gap-2 text-sm font-medium text-foreground">
                            Version {v.version}
                            {i === 0 && (
                              <Badge tone="primary" size="sm">
                                Latest
                              </Badge>
                            )}
                            {loaded?.version === v.version && <Badge size="sm">On canvas</Badge>}
                            {v.is_archived && <Badge size="sm">Archived</Badge>}
                          </p>
                          <p className="text-xs text-foreground-secondary">
                            {v.created_at} · {v.anchor_count} anchors · {v.field_count} fields
                          </p>
                        </div>
                        <Button variant="outline" size="sm" onClick={() => loadTemplate(loaded?.template_id ?? templateId, v.version)}>
                          Open
                        </Button>
                      </li>
                    ))}
                  </ul>
                  <p className="m-4 flex items-start gap-2 rounded-md bg-surface-sunken px-3 py-2 text-xs text-foreground-secondary">
                    <Lock className="mt-0.5 size-4 shrink-0" aria-hidden /> Saving always creates a new version. Earlier versions stay exactly as they were, so past results can always be reproduced.
                  </p>
                </div>
              ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

const TOOLS = [
  [null, 'Select', MousePointer2, 'V'],
  ['field', 'Field', Square, 'F'],
  ['anchor', 'Anchor', SquareDashed, 'A'],
];

function PanelHint({ children }) {
  return (
    <p className="m-4 flex items-start gap-2 rounded-md border border-dashed border-border-strong px-3 py-3 text-sm text-foreground-secondary">
      <FileImage className="mt-0.5 size-4 shrink-0 text-foreground-subtle" aria-hidden /> <span>{children}</span>
    </p>
  );
}

function ValidatorChip({ box }) {
  if (box.checksum) return <Badge size="sm">Check digit</Badge>;
  if (box.validationRegex) return <Badge size="sm">Format rule</Badge>;
  return (
    <Badge tone="warning" size="sm" title="With no format rule this field can never be approved automatically">
      No rule
    </Badge>
  );
}

function FieldRow({ box, selected, onSelect, onChange, onDelete }) {
  const ref = useRef(null);
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selected]);
  return (
    <div ref={ref} className={cn('border-b border-border', selected && 'bg-primary-surface')}>
      <button onClick={onSelect} aria-expanded={selected} className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-sunken">
        <Square className={cn('size-4 shrink-0', selected ? 'text-primary' : 'text-foreground-subtle')} aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-foreground">{box.label || box.fieldId}</span>
          <span className="block truncate font-mono text-xs text-foreground-secondary">{box.fieldId}</span>
        </span>
        <ValidatorChip box={box} />
      </button>
      {selected && (
        <div className="grid grid-cols-2 gap-3 px-4 pt-1 pb-4">
          <Label>
            Field name
            <Input mono value={box.fieldId} onChange={(e) => onChange({ fieldId: e.target.value })} />
          </Label>
          <Label>
            Display label
            <Input value={box.label} onChange={(e) => onChange({ label: e.target.value })} placeholder="Shipper Name" />
          </Label>
          <Label className="col-span-2">
            Format rule (optional)
            <Input mono value={box.validationRegex} onChange={(e) => onChange({ validationRegex: e.target.value })} placeholder="e.g. ^[A-Z]{3}$" />
          </Label>
          <Label>
            Field type
            <NativeSelect value={box.dataType} onChange={(e) => onChange({ dataType: e.target.value })}>
              <option value="string">Text</option>
              <option value="enum">One of a fixed list</option>
            </NativeSelect>
          </Label>
          <Label>
            Minimum confidence (0–1)
            <Input mono type="number" min="0" max="1" step="0.01" value={box.confidenceFloor} onChange={(e) => onChange({ confidenceFloor: e.target.value })} />
          </Label>
          {box.checksum && <p className="col-span-2 text-xs text-foreground-secondary">Check digit "{box.checksum}" is kept when you save.</p>}
          {!box.validationRegex && !box.checksum && (
            <p className="col-span-2 text-xs text-warning">Without a format rule, every read of this field goes to a spot check and is never approved automatically.</p>
          )}
          <Button variant="ghost" size="sm" className="col-span-2 justify-self-start text-danger hover:text-danger-hover" onClick={onDelete}>
            <Trash2 /> Delete box <Kbd>Del</Kbd>
          </Button>
        </div>
      )}
    </div>
  );
}

function AnchorRow({ box, selected, onSelect, onChange, onDelete }) {
  return (
    <div className={cn('border-b border-border', selected && 'bg-primary-surface')}>
      <button onClick={onSelect} aria-expanded={selected} className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-sunken">
        <SquareDashed className={cn('size-4 shrink-0', selected ? 'text-primary' : 'text-foreground-subtle')} aria-hidden />
        <span className={cn('min-w-0 flex-1 truncate text-sm', box.text ? 'font-medium text-foreground' : 'text-danger')}>{box.text || 'Needs its printed text'}</span>
        <span className="text-xs text-foreground-secondary">Match {Math.round(Number(box.matchThreshold) * 100)}%</span>
      </button>
      {selected && (
        <div className="grid grid-cols-[1fr_112px] gap-3 px-4 pb-4">
          <Label>
            Printed text
            <Input value={box.text} onChange={(e) => onChange({ text: e.target.value })} placeholder="Not Negotiable Air Waybill" />
          </Label>
          <Label>
            Match (0–1)
            <Input mono type="number" min="0" max="1" step="0.01" value={box.matchThreshold} onChange={(e) => onChange({ matchThreshold: e.target.value })} />
          </Label>
          <Button variant="ghost" size="sm" className="justify-self-start text-danger hover:text-danger-hover" onClick={onDelete}>
            <Trash2 /> Delete box
          </Button>
        </div>
      )}
    </div>
  );
}
