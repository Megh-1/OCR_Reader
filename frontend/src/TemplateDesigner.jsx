import { useEffect, useRef, useState } from 'react';
import { Rect } from 'fabric';
import { rectToRelativeBBox } from './geometry';
import { useDocumentCanvas } from './useDocumentCanvas';
import { ApiValidationError, createTemplate, formatErrorDetail } from './api';

const CANVAS_W = 900;
const CANVAS_H = 700;
const MIN_BOX_SIZE = 6; // px in canvas space; drags smaller than this are treated as accidental clicks
const SAMPLE_IMAGE_URL = '/sample-document.png';

const ANCHOR_STYLE = { stroke: '#e67e22', fill: 'rgba(230,126,34,0.18)', strokeDashArray: [6, 4] };
const FIELD_STYLE = { stroke: '#2980b9', fill: 'rgba(41,128,185,0.15)' };

let nextFieldSeq = 1;

function createDefaultBoxRecord(id, type) {
  if (type === 'anchor') {
    return { id, type: 'anchor', text: '', matchThreshold: 0.85 };
  }
  return {
    id,
    type: 'field',
    fieldId: `field_${nextFieldSeq++}`,
    label: '',
    dataType: 'string',
    validationRegex: '',
    confidenceFloor: 0.75,
  };
}

export default function TemplateDesigner() {
  const rectsRef = useRef(new Map()); // boxId -> fabric.Rect
  const drawModeRef = useRef(null);

  const [drawMode, setDrawMode] = useState(null); // null | 'anchor' | 'field'
  const [boxes, setBoxes] = useState([]);
  const [templateId, setTemplateId] = useState('bits_goa_id_card');
  const [submitState, setSubmitState] = useState({ status: 'idle' });

  const { canvasElRef, fabricCanvasRef, imageRef, imageReady } = useDocumentCanvas(SAMPLE_IMAGE_URL, CANVAS_W, CANVAS_H);

  // Updates the ref synchronously, in the same tick as the click that
  // changes it - the event handlers below read drawModeRef.current, and a
  // React state update alone wouldn't reach it until after the next render
  // commits. A mousedown arriving before that commit (a fast click-then-drag)
  // would otherwise see a stale mode and silently fall through to Fabric's
  // default marquee-selection behavior instead of drawing a box.
  function setMode(mode) {
    drawModeRef.current = mode;
    setDrawMode(mode);
  }

  // Interactive box-drawing, layered on top of the shared canvas+image setup
  // from useDocumentCanvas. That hook's own effect runs first (it's called
  // above, earlier in this component), so fabricCanvasRef.current is always
  // already populated by the time this effect body runs.
  useEffect(() => {
    const canvas = fabricCanvasRef.current;
    if (!canvas) return;

    let drawingRect = null;
    let startPoint = null;

    function handleMouseDown(opt) {
      const mode = drawModeRef.current;
      if (!mode) return;
      const pointer = canvas.getScenePoint(opt.e);
      startPoint = pointer;
      const style = mode === 'anchor' ? ANCHOR_STYLE : FIELD_STYLE;
      drawingRect = new Rect({
        left: pointer.x,
        top: pointer.y,
        width: 0,
        height: 0,
        originX: 'left',
        originY: 'top',
        strokeWidth: 2,
        ...style,
      });
      canvas.add(drawingRect);
    }

    function handleMouseMove(opt) {
      if (!drawingRect || !startPoint) return;
      const pointer = canvas.getScenePoint(opt.e);
      drawingRect.set({
        left: Math.min(pointer.x, startPoint.x),
        top: Math.min(pointer.y, startPoint.y),
        width: Math.abs(pointer.x - startPoint.x),
        height: Math.abs(pointer.y - startPoint.y),
      });
      canvas.requestRenderAll();
    }

    function handleMouseUp() {
      const finishedRect = drawingRect;
      drawingRect = null;
      startPoint = null;
      if (!finishedRect) return;

      if (finishedRect.width < MIN_BOX_SIZE || finishedRect.height < MIN_BOX_SIZE) {
        canvas.remove(finishedRect);
        canvas.requestRenderAll();
        return;
      }

      const mode = drawModeRef.current;
      const id = crypto.randomUUID();
      rectsRef.current.set(id, finishedRect);
      setBoxes((prev) => [...prev, createDefaultBoxRecord(id, mode)]);
      setMode(null); // one box per click of "Draw anchor/field", then back to select
    }

    canvas.on('mouse:down', handleMouseDown);
    canvas.on('mouse:move', handleMouseMove);
    canvas.on('mouse:up', handleMouseUp);

    return () => {
      canvas.off('mouse:down', handleMouseDown);
      canvas.off('mouse:move', handleMouseMove);
      canvas.off('mouse:up', handleMouseUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function updateBox(id, patch) {
    setBoxes((prev) => prev.map((b) => (b.id === id ? { ...b, ...patch } : b)));
  }

  function deleteBox(id) {
    const rect = rectsRef.current.get(id);
    if (rect) {
      fabricCanvasRef.current.remove(rect);
      fabricCanvasRef.current.requestRenderAll();
      rectsRef.current.delete(id);
    }
    setBoxes((prev) => prev.filter((b) => b.id !== id));
  }

  const anchorCount = boxes.filter((b) => b.type === 'anchor').length;
  const canSubmit = imageReady && anchorCount >= 1 && templateId.trim().length > 0;

  async function handleSubmit() {
    if (!canSubmit) {
      setSubmitState({
        status: 'error',
        messages: [
          !templateId.trim() ? 'Template ID is required.' : null,
          anchorCount < 1 ? 'At least one anchor box is required before submitting.' : null,
        ].filter(Boolean),
      });
      return;
    }

    const image = imageRef.current;
    const anchors = [];
    const fields = [];

    for (const box of boxes) {
      const rect = rectsRef.current.get(box.id);
      if (!rect) continue;
      const bbox = rectToRelativeBBox(rect, image);

      if (box.type === 'anchor') {
        anchors.push({ text: box.text, bbox, match_threshold: Number(box.matchThreshold) });
      } else {
        fields.push({
          id: box.fieldId,
          label: box.label,
          bbox,
          data_type: box.dataType,
          validation_regex: box.validationRegex ? box.validationRegex : null,
          confidence_floor: Number(box.confidenceFloor),
        });
      }
    }

    setSubmitState({ status: 'submitting' });
    try {
      // Only what the backend's TemplateIn schema actually accepts.
      // version/created_at are server-assigned - sending client guesses
      // for them would misrepresent who the source of truth is.
      const result = await createTemplate({ template_id: templateId.trim(), anchors, fields });
      setSubmitState({ status: 'success', result });
    } catch (err) {
      if (err instanceof ApiValidationError) {
        setSubmitState({ status: 'error', messages: formatErrorDetail(err.detail) });
      } else {
        setSubmitState({ status: 'error', messages: [String(err)] });
      }
    }
  }

  return (
    <div className="designer">
      <div className="canvas-pane">
        <div className="toolbar">
          <button
            className={drawMode === 'anchor' ? 'active anchor' : 'anchor'}
            onClick={() => setMode(drawMode === 'anchor' ? null : 'anchor')}
          >
            {drawMode === 'anchor' ? 'Drawing anchor… (click to cancel)' : '+ Draw anchor'}
          </button>
          <button
            className={drawMode === 'field' ? 'active field' : 'field'}
            onClick={() => setMode(drawMode === 'field' ? null : 'field')}
          >
            {drawMode === 'field' ? 'Drawing field… (click to cancel)' : '+ Draw field'}
          </button>
        </div>
        <canvas ref={canvasElRef} />
      </div>

      <div className="side-panel">
        <label className="template-id-label">
          Template ID
          <input
            type="text"
            value={templateId}
            onChange={(e) => setTemplateId(e.target.value)}
            placeholder="e.g. bits_goa_id_card"
          />
        </label>

        <div className="box-list">
          {boxes.length === 0 && <p className="hint">Draw at least one anchor box on the document.</p>}
          {boxes.map((box) =>
            box.type === 'anchor' ? (
              <div key={box.id} className="box-card anchor">
                <div className="box-card-header">
                  <span>Anchor</span>
                  <button className="delete" onClick={() => deleteBox(box.id)}>
                    ×
                  </button>
                </div>
                <label>
                  Anchor text
                  <input
                    type="text"
                    value={box.text}
                    onChange={(e) => updateBox(box.id, { text: e.target.value })}
                    placeholder="e.g. BITS PILANI"
                  />
                </label>
                <label>
                  Match threshold
                  <input
                    type="number"
                    min="0"
                    max="1"
                    step="0.01"
                    value={box.matchThreshold}
                    onChange={(e) => updateBox(box.id, { matchThreshold: e.target.value })}
                  />
                </label>
              </div>
            ) : (
              <div key={box.id} className="box-card field">
                <div className="box-card-header">
                  <span>Field</span>
                  <button className="delete" onClick={() => deleteBox(box.id)}>
                    ×
                  </button>
                </div>
                <label>
                  Field ID
                  <input
                    type="text"
                    value={box.fieldId}
                    onChange={(e) => updateBox(box.id, { fieldId: e.target.value })}
                  />
                </label>
                <label>
                  Label
                  <input
                    type="text"
                    value={box.label}
                    onChange={(e) => updateBox(box.id, { label: e.target.value })}
                    placeholder="e.g. Student Name"
                  />
                </label>
                <label>
                  Data type
                  <select value={box.dataType} onChange={(e) => updateBox(box.id, { dataType: e.target.value })}>
                    <option value="string">string</option>
                    <option value="enum">enum</option>
                  </select>
                </label>
                <label>
                  Validation regex (optional)
                  <input
                    type="text"
                    value={box.validationRegex}
                    onChange={(e) => updateBox(box.id, { validationRegex: e.target.value })}
                    placeholder="e.g. ^[A-Za-z ]{2,60}$"
                  />
                </label>
                <label>
                  Confidence floor
                  <input
                    type="number"
                    min="0"
                    max="1"
                    step="0.01"
                    value={box.confidenceFloor}
                    onChange={(e) => updateBox(box.id, { confidenceFloor: e.target.value })}
                  />
                </label>
              </div>
            ),
          )}
        </div>

        <button className="submit" disabled={!canSubmit || submitState.status === 'submitting'} onClick={handleSubmit}>
          {submitState.status === 'submitting' ? 'Saving…' : 'Save template'}
        </button>
        {!canSubmit && (
          <p className="hint">
            {anchorCount < 1 ? 'At least one anchor box is required. ' : ''}
            {!templateId.trim() ? 'Template ID is required.' : ''}
          </p>
        )}

        {submitState.status === 'error' && (
          <div className="banner error">
            <strong>Save failed:</strong>
            <ul>
              {submitState.messages.map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
          </div>
        )}
        {submitState.status === 'success' && (
          <div className="banner success">
            <strong>Saved.</strong> {submitState.result.template_id} v{submitState.result.version} (created{' '}
            {submitState.result.created_at})
          </div>
        )}
      </div>
    </div>
  );
}
