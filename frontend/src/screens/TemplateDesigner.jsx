import { AnimatePresence, motion } from 'framer-motion';
import { FabricText, Rect, Shadow } from 'fabric';
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
import { PageHeader } from '@/components/bits';
import { Button } from '@/components/ui/button';
import { Kbd } from '@/components/ui/card';
import { Input, Label, NativeSelect } from '@/components/ui/input';
import { rectToRelativeBBox, relativeBBoxToRect } from '@/geometry';
import { useDocumentCanvas } from '@/useDocumentCanvas';
import { cn, timeAgo } from '@/lib/utils';

const MIN_BOX_SIZE = 6; // px in canvas space; drags smaller than this are accidental clicks
const SAMPLE_IMAGE_URL = '/sample-document.png';
const INK = '#15181c';
// Template boxes are ink with a paper halo, never the accent: on a document, orange is
// reserved for a field under review.
const FIELD_STYLE = { stroke: INK, fill: 'rgba(21,24,28,0.10)', strokeWidth: 2, strokeUniform: true };
const ANCHOR_STYLE = { stroke: INK, fill: 'rgba(21,24,28,0.04)', strokeWidth: 2, strokeDashArray: [7, 4], strokeUniform: true };
const CONTROLS = {
  borderColor: INK,
  cornerColor: '#ffffff',
  cornerStrokeColor: INK,
  transparentCorners: false,
  cornerSize: 8,
  cornerStyle: 'rect',
  borderScaleFactor: 1.5,
  shadow: new Shadow({ color: 'rgba(255,255,255,0.95)', blur: 3 }),
};

let nextFieldSeq = 1;
function newBox(id, type) {
  if (type === 'anchor') return { id, type: 'anchor', text: '', matchThreshold: 0.7 };
  return { id, type: 'field', fieldId: `field_${nextFieldSeq++}`, label: '', dataType: 'string', validationRegex: '', confidenceFloor: 0.6, checksum: null };
}

const fmtBox = (b) => (b ? b.map((n) => n.toFixed(4)).join('  ') : '—');

export default function TemplateDesigner() {
  const [size] = useState(() => ({
    w: Math.max(560, Math.min(1100, window.innerWidth - 232 - 420 - 48)),
    h: Math.max(520, window.innerHeight - 92 - 56 - 44 - 24),
  }));
  const [imageUrl, setImageUrl] = useState(SAMPLE_IMAGE_URL);
  const { canvasElRef, fabricCanvasRef, imageRef, imageReady } = useDocumentCanvas(imageUrl, size.w, size.h);

  const rectsRef = useRef(new Map()); // boxId -> fabric Rect
  const labelsRef = useRef(new Map()); // boxId -> fabric Text
  const modeRef = useRef(null);
  const cursorRef = useRef(null);
  const pendingLoadRef = useRef(null); // a template waiting for its page image to finish loading
  const hintsRef = useRef(null);
  const uploadedUrlRef = useRef(null);

  const [mode, setModeState] = useState(null); // null | 'field' | 'anchor'
  const [boxes, setBoxes] = useState([]);
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

  const labelFor = useCallback((box) => (box.type === 'anchor' ? `⌖ ${box.text || 'anchor'}` : box.fieldId), []);

  const syncLabel = useCallback((boxId) => {
    const rect = rectsRef.current.get(boxId);
    const label = labelsRef.current.get(boxId);
    if (!rect || !label) return;
    label.set({ left: rect.left - 1, top: rect.top - label.height - 1 });
  }, []);

  const addRect = useCallback(
    (boxId, rect, box) => {
      const canvas = fabricCanvasRef.current;
      rect.set(CONTROLS);
      rectsRef.current.set(boxId, rect);
      const label = new FabricText(labelFor(box), {
        fontFamily: 'IBM Plex Mono',
        fontSize: 10,
        fill: '#ffffff',
        backgroundColor: INK,
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
        place(a.bbox, ANCHOR_STYLE, { id: crypto.randomUUID(), type: 'anchor', text: a.text, matchThreshold: a.match_threshold });
      }
      for (const f of template.fields) {
        place(f.bbox, FIELD_STYLE, {
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
      // Draw it over a real page of that type when one has been processed, not over the placeholder.
      const page = results.find((r) => r.template_id === id && r.has_image);
      const url = page ? resultImageUrl(page.id) : imageUrl;
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
      drawing = new Rect({ left: start.x, top: start.y, width: 0, height: 0, originX: 'left', originY: 'top', ...(m === 'anchor' ? ANCHOR_STYLE : FIELD_STYLE) });
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
  const canSave = imageReady && anchors.length >= 1 && templateId.trim().length > 0;

  async function handleSave() {
    const image = imageRef.current;
    const payload = { template_id: templateId.trim(), anchors: [], fields: [], ...(hintsRef.current ? { hints: hintsRef.current } : {}) };
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
        eyebrow="01 — Template designer"
        title={loaded ? loaded.template_id : 'New template'}
        meta={loaded ? `editing v${loaded.version} · saving writes v${nextVersion} — versions are append-only, nothing is overwritten` : 'draw anchors and fields on a sample document, then save a config'}
      >
        <NativeSelect className="w-52" value="" onChange={(e) => loadTemplate(e.target.value)}>
          <option value="">Open template…</option>
          {templates.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect
          className="w-56"
          value=""
          onChange={(e) => {
            const v = e.target.value;
            if (v === 'sample') switchImage(SAMPLE_IMAGE_URL);
            else if (v) switchImage(resultImageUrl(v));
          }}
        >
          <option value="">Document from…</option>
          <option value="sample">Sample card</option>
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
          <label className="cursor-pointer">
            <Upload /> Upload
            <input
              type="file"
              accept="image/*"
              className="hidden"
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

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_420px]">
        {/* ---------------------------------------------------------------- canvas */}
        <div className="flex min-h-0 flex-col">
          <div className="flex h-14 items-center gap-3 border-b border-border px-5">
            <div className="flex rounded-md border border-border-strong p-0.5">
              {[
                [null, 'Select', MousePointer2, 'V'],
                ['field', 'Field', Square, 'F'],
                ['anchor', 'Anchor', SquareDashed, 'A'],
              ].map(([m, label, Icon, key]) => (
                <button
                  key={label}
                  onClick={() => setMode(m)}
                  className={cn(
                    'relative flex h-8 items-center gap-2 rounded-sm px-3 text-[12.5px] font-medium transition-colors',
                    mode === m ? 'text-primary-foreground' : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  {mode === m && <motion.span layoutId="tool" className="absolute inset-0 rounded-sm bg-primary" transition={{ duration: 0.18 }} />}
                  <Icon className="relative size-4" />
                  <span className="relative">{label}</span>
                  <Kbd className={cn('relative', mode === m && 'border-graphite-400 bg-transparent text-primary-foreground')}>{key}</Kbd>
                </button>
              ))}
            </div>
            <AnimatePresence>
              {mode && (
                <motion.span
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex items-center gap-2 text-[12px] text-secondary-foreground"
                >
                  <Crosshair className="size-4" /> Drag on the document to draw {mode === 'anchor' ? 'an anchor — a printed label identical on every document of this type' : 'a field box'}
                </motion.span>
              )}
            </AnimatePresence>
            <div className="ml-auto flex items-center gap-4 font-mono text-[11px] text-muted-foreground">
              <span className="caps-sm text-faint-foreground">cursor</span>
              <span ref={cursorRef} className="w-28 text-foreground">—</span>
            </div>
          </div>

          <div className="bed-grid relative min-h-0 flex-1 overflow-auto p-6">
            <div className="mx-auto w-fit shadow-[0_24px_64px_-12px_rgba(0,0,0,0.7)]">
              <canvas ref={canvasElRef} />
            </div>
          </div>

          <div className="flex h-11 items-center gap-6 border-t border-border bg-card px-5 font-mono text-[11px]">
            <span className="caps-sm text-faint-foreground">{readout?.live ? 'measuring' : 'selection'}</span>
            <span className="text-foreground">{fmtBox(readout?.bbox)}</span>
            {readout?.bbox && (
              <span className="text-muted-foreground">
                w {(readout.bbox[2] - readout.bbox[0]).toFixed(4)} · h {(readout.bbox[3] - readout.bbox[1]).toFixed(4)}
              </span>
            )}
            <span className="ml-auto text-faint-foreground">
              {anchors.length} anchor{anchors.length === 1 ? '' : 's'} · {fields.length} fields · boxes are page-relative fractions
            </span>
          </div>
        </div>

        {/* ---------------------------------------------------------------- panel */}
        <div className="flex min-h-0 flex-col border-l border-border bg-card">
          <div className="flex flex-col gap-3 border-b border-border p-4">
            <div className="flex items-end gap-2">
              <Label className="flex-1">
                Template id
                <Input mono value={templateId} onChange={(e) => setTemplateId(e.target.value)} placeholder="e.g. air_waybill" />
              </Label>
              <Button variant="primary" disabled={!canSave || save.status === 'saving'} onClick={handleSave}>
                <Save /> {save.status === 'saving' ? 'Saving…' : `Save v${nextVersion}`}
              </Button>
            </div>
            <AnimatePresence mode="wait">
              {save.status === 'saved' && (
                <motion.div key="ok" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="flex items-center gap-2 rounded-md border border-border-strong bg-muted px-3 py-2 text-[12px]">
                  <GitCommitHorizontal className="size-4 text-icon" />
                  Saved <span className="font-mono">{save.result.template_id} v{save.result.version}</span>
                  <span className="text-muted-foreground">— earlier versions untouched</span>
                </motion.div>
              )}
              {save.status === 'error' && (
                <motion.div key="err" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="rounded-md border border-tier-review bg-tier-review-surface px-3 py-2 text-[12px] text-signal-300">
                  {save.messages.map((m) => (
                    <div key={m}>{m}</div>
                  ))}
                </motion.div>
              )}
              {!canSave && save.status !== 'error' && save.status !== 'saved' && (
                <div className="text-[12px] text-muted-foreground">
                  {anchors.length < 1 ? 'Needs at least one anchor. ' : ''}
                  {!templateId.trim() ? 'Needs a template id.' : ''}
                </div>
              )}
            </AnimatePresence>
          </div>

          <div className="flex border-b border-border px-2">
            {[
              ['fields', `Fields ${fields.length}`],
              ['anchors', `Anchors ${anchors.length}`],
              ['versions', `Versions ${versions.length || ''}`],
            ].map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={cn('relative h-10 px-3 text-[12.5px] font-medium', tab === key ? 'text-foreground' : 'text-muted-foreground hover:text-foreground')}
              >
                {label}
                {tab === key && <motion.span layoutId="designer-tab" className="absolute inset-x-2 bottom-0 h-[2px] bg-foreground" />}
              </button>
            ))}
          </div>

          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
            {tab === 'fields' &&
              (fields.length === 0 ? (
                <PanelHint>Press F and drag over a value on the document. Each box becomes a field in the config.</PanelHint>
              ) : (
                fields.map((b) => (
                  <FieldRow key={b.id} box={b} selected={b.id === selectedId} onSelect={() => selectBox(b.id)} onChange={(p) => update(b.id, p)} onDelete={() => deleteBox(b.id)} />
                ))
              ))}
            {tab === 'anchors' &&
              (anchors.length === 0 ? (
                <PanelHint>Press A and box a printed form label - Module 5 requires every anchor to match before it trusts a document type.</PanelHint>
              ) : (
                anchors.map((b) => (
                  <AnchorRow key={b.id} box={b} selected={b.id === selectedId} onSelect={() => selectBox(b.id)} onChange={(p) => update(b.id, p)} onDelete={() => deleteBox(b.id)} />
                ))
              ))}
            {tab === 'versions' &&
              (versions.length === 0 ? (
                <PanelHint>Open or save a template to see its history.</PanelHint>
              ) : (
                <div className="relative py-2">
                  <div className="absolute top-4 bottom-4 left-[27px] w-px bg-border-strong" />
                  {versions.map((v, i) => (
                    <div key={v.version} className="relative flex items-center gap-3 px-4 py-2.5">
                      <span className={cn('relative z-10 size-[9px] rounded-full border-2 bg-card', i === 0 ? 'border-foreground' : 'border-graphite-600')} />
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-[13px] font-semibold">v{v.version}</span>
                          {i === 0 && <span className="caps-sm rounded-sm border border-border-strong px-1.5 text-secondary-foreground">latest</span>}
                          {loaded?.version === v.version && <span className="caps-sm text-muted-foreground">on canvas</span>}
                          {v.is_archived && <span className="caps-sm text-faint-foreground">archived</span>}
                        </div>
                        <div className="font-mono text-[11px] text-faint-foreground">
                          {v.created_at} · {v.anchor_count} anchors · {v.field_count} fields
                        </div>
                      </div>
                      <Button variant="ghost" size="sm" onClick={() => loadTemplate(loaded?.template_id ?? templateId, v.version)}>
                        Open
                      </Button>
                    </div>
                  ))}
                  <div className="mx-4 mt-2 flex items-start gap-2 rounded-md border border-border bg-muted px-3 py-2 text-[11.5px] text-muted-foreground">
                    <Lock className="mt-0.5 size-3.5 shrink-0" /> Append-only: saving always writes a new version. Past results stay reproducible against the exact version that produced them.
                  </div>
                </div>
              ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function PanelHint({ children }) {
  return (
    <div className="m-4 flex items-start gap-2 rounded-md border border-dashed border-border-strong px-3 py-3 text-[12.5px] text-muted-foreground">
      <FileImage className="mt-0.5 size-4 shrink-0 text-icon-muted" /> {children}
    </div>
  );
}

function ValidatorChip({ box }) {
  if (box.checksum)
    return <span className="caps-sm rounded-sm border border-border-strong px-1.5 py-0.5 text-secondary-foreground">checksum</span>;
  if (box.validationRegex)
    return <span className="caps-sm rounded-sm border border-border-strong px-1.5 py-0.5 text-secondary-foreground">regex</span>;
  return (
    <span className="caps-sm rounded-sm border border-tier-spot-border px-1.5 py-0.5 text-tier-spot-foreground" title="With no regex or checksum this field can never be auto-approved">
      conf. only
    </span>
  );
}

function FieldRow({ box, selected, onSelect, onChange, onDelete }) {
  const ref = useRef(null);
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selected]);
  return (
    <motion.div ref={ref} layout className={cn('border-b border-border', selected && 'bg-accent')}>
      <button onClick={onSelect} className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-muted">
        <Square className={cn('size-3.5 shrink-0', selected ? 'text-foreground' : 'text-icon-muted')} />
        <div className="min-w-0 flex-1">
          <div className="truncate font-mono text-[12.5px]">{box.fieldId}</div>
          <div className="truncate text-[11.5px] text-muted-foreground">{box.label || 'no label'}</div>
        </div>
        <ValidatorChip box={box} />
        <span className="w-9 text-right font-mono text-[11px] text-faint-foreground">≥{Math.round(Number(box.confidenceFloor) * 100)}</span>
      </button>
      <AnimatePresence initial={false}>
        {selected && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="grid grid-cols-2 gap-3 px-4 pt-1 pb-4">
              <Label>
                Field id
                <Input mono value={box.fieldId} onChange={(e) => onChange({ fieldId: e.target.value })} />
              </Label>
              <Label>
                Label
                <Input value={box.label} onChange={(e) => onChange({ label: e.target.value })} placeholder="Shipper Name" />
              </Label>
              <Label className="col-span-2">
                Validation regex
                <Input mono value={box.validationRegex} onChange={(e) => onChange({ validationRegex: e.target.value })} placeholder="^[A-Z]{3}$  — empty means confidence only" />
              </Label>
              <Label>
                Data type
                <NativeSelect value={box.dataType} onChange={(e) => onChange({ dataType: e.target.value })}>
                  <option value="string">string</option>
                  <option value="enum">enum</option>
                </NativeSelect>
              </Label>
              <Label>
                Confidence floor
                <Input mono type="number" min="0" max="1" step="0.01" value={box.confidenceFloor} onChange={(e) => onChange({ confidenceFloor: e.target.value })} />
              </Label>
              {box.checksum && <div className="col-span-2 font-mono text-[11px] text-muted-foreground">checksum {box.checksum} · kept on save</div>}
              {!box.validationRegex && !box.checksum && (
                <div className="col-span-2 text-[11.5px] text-tier-spot-foreground">No validator: this field can never be auto-approved — every read goes to a spot check.</div>
              )}
              <Button variant="ghost" size="sm" className="col-span-2 justify-self-start text-tier-review-text hover:text-signal-300" onClick={onDelete}>
                <Trash2 /> Delete box <Kbd>Del</Kbd>
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function AnchorRow({ box, selected, onSelect, onChange, onDelete }) {
  return (
    <div className={cn('border-b border-border', selected && 'bg-accent')}>
      <button onClick={onSelect} className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-muted">
        <SquareDashed className="size-3.5 shrink-0 text-icon-muted" />
        <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">{box.text || 'untitled anchor'}</span>
        <span className="font-mono text-[11px] text-faint-foreground">≥{Math.round(Number(box.matchThreshold) * 100)}</span>
      </button>
      {selected && (
        <div className="grid grid-cols-[1fr_110px] gap-3 px-4 pb-4">
          <Label>
            Anchor text
            <Input value={box.text} onChange={(e) => onChange({ text: e.target.value })} placeholder="Not Negotiable Air Waybill" />
          </Label>
          <Label>
            Match
            <Input mono type="number" min="0" max="1" step="0.01" value={box.matchThreshold} onChange={(e) => onChange({ matchThreshold: e.target.value })} />
          </Label>
          <Button variant="ghost" size="sm" className="justify-self-start text-tier-review-text" onClick={onDelete}>
            <Trash2 /> Delete
          </Button>
        </div>
      )}
    </div>
  );
}
