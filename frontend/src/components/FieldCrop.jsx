import { useEffect, useRef, useState } from 'react';
import { cn } from '@/lib/utils';

// Canvas drawing can't use CSS classes, so read the design tokens at draw time.
const TIER_TOKEN = { auto: '--success-solid', spot_check: '--warning-solid', review: '--danger-solid' };
const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/**
 * The original document region a value was read from: the stored page render (Module 8),
 * cropped to the field's relative bbox plus context, drawn on a canvas at device resolution,
 * with the box outlined in its tier's intensity (dashed for a spot check, solid for review).
 * `mode="page"` draws the whole page instead, as a locator for where the field sits.
 */
export function FieldCrop({ src, bbox, tier, mode = 'crop', className, contextFrac = 0.35 }) {
  const wrapRef = useRef(null);
  const canvasRef = useRef(null);
  const [img, setImg] = useState(null);
  const [failed, setFailed] = useState(false);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    setImg(null);
    setFailed(false);
    const image = new Image();
    image.onload = () => setImg(image);
    image.onerror = () => setFailed(true);
    image.src = src;
  }, [src]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !img || !width || !bbox) return;
    const [x1, y1, x2, y2] = bbox;
    const W = img.naturalWidth;
    const H = img.naturalHeight;
    let sx, sy, sw, sh;
    if (mode === 'page') {
      [sx, sy, sw, sh] = [0, 0, W, H];
    } else {
      const bw = (x2 - x1) * W;
      const bh = (y2 - y1) * H;
      const padX = Math.max(bw * contextFrac, 24);
      const padY = Math.max(bh * 1.4, 28);
      sx = Math.max(0, x1 * W - padX);
      sy = Math.max(0, y1 * H - padY);
      sw = Math.min(W - sx, bw + 2 * padX);
      sh = Math.min(H - sy, bh + 2 * padY);
    }
    const dpr = window.devicePixelRatio || 1;
    const cssW = width;
    const cssH = Math.round((cssW * sh) / sw);
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, cssW, cssH);

    const k = cssW / sw;
    const rx = (x1 * W - sx) * k;
    const ry = (y1 * H - sy) * k;
    const rw = (x2 - x1) * W * k;
    const rh = (y2 - y1) * H * k;
    const stroke = token(TIER_TOKEN[tier] ?? '--foreground');
    if (mode === 'page') {
      // dim everything but the field
      ctx.fillStyle = 'rgba(17,24,39,0.45)';
      ctx.fillRect(0, 0, cssW, cssH);
      ctx.drawImage(img, x1 * W, y1 * H, (x2 - x1) * W, (y2 - y1) * H, rx, ry, rw, rh);
    }
    ctx.lineWidth = mode === 'page' ? 1.5 : 2;
    ctx.strokeStyle = token('--surface');
    ctx.strokeRect(rx - 2, ry - 2, rw + 4, rh + 4);
    ctx.strokeStyle = stroke;
    ctx.setLineDash(tier === 'spot_check' ? [6, 4] : []);
    ctx.strokeRect(rx - 1, ry - 1, rw + 2, rh + 2);
    ctx.setLineDash([]);
  }, [img, width, bbox, tier, mode, contextFrac]);

  return (
    <div ref={wrapRef} className={cn('relative w-full', className)}>
      {failed ? (
        <div className="grid h-32 place-items-center text-xs text-foreground-subtle">No page image was saved for this document</div>
      ) : (
        <canvas ref={canvasRef} className={cn('block w-full', !img && 'h-32 bg-surface-sunken')} />
      )}
    </div>
  );
}
