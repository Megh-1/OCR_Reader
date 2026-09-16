/**
 * Fit-to-contain placement for a `naturalW x naturalH` image inside a
 * `canvasW x canvasH` canvas, preserving aspect ratio and centering it.
 * Unless the canvas and image share an aspect ratio exactly, this leaves
 * letterboxing on one axis - the image's actual displayed region is a
 * sub-rectangle of the canvas, not the whole canvas.
 */
export function computeContainFit(naturalW, naturalH, canvasW, canvasH) {
  const scale = Math.min(canvasW / naturalW, canvasH / naturalH);
  const displayedW = naturalW * scale;
  const displayedH = naturalH * scale;
  return {
    scale,
    left: (canvasW - displayedW) / 2,
    top: (canvasH - displayedH) / 2,
    displayedW,
    displayedH,
  };
}

const clamp01 = (v) => Math.min(1, Math.max(0, v));

/**
 * Converts a drawn Fabric rect into a relative [x1,y1,x2,y2] bbox (0.0-1.0)
 * of the SAMPLE IMAGE - not the canvas.
 *
 * This is deliberately divided by the image's own rendered position/size
 * (`image.left/top/getScaledWidth()/getScaledHeight()`), never by
 * `canvas.getWidth()/getHeight()`. The canvas has a fixed logical size
 * chosen for a consistent editor layout; the document image is scaled to
 * fit *within* it (see computeContainFit), which almost always leaves
 * letterboxing on one axis. Dividing by the canvas's own dimensions would
 * silently fold that letterboxing margin into every coordinate - a box
 * drawn flush against the image's left edge would not come out at x1=0.
 *
 * Both `rect` and `image` must share the canvas's coordinate space (i.e.
 * neither is nested inside a group with its own transform), and both use
 * originX/originY = 'left'/'top', so `.left/.top` is unambiguously each
 * object's top-left corner in that space.
 */
export function rectToRelativeBBox(rect, image) {
  const imgLeft = image.left;
  const imgTop = image.top;
  const imgWidth = image.getScaledWidth();
  const imgHeight = image.getScaledHeight();

  const rectLeft = rect.left;
  const rectTop = rect.top;
  const rectWidth = rect.getScaledWidth();
  const rectHeight = rect.getScaledHeight();

  return [
    clamp01((rectLeft - imgLeft) / imgWidth),
    clamp01((rectTop - imgTop) / imgHeight),
    clamp01((rectLeft + rectWidth - imgLeft) / imgWidth),
    clamp01((rectTop + rectHeight - imgTop) / imgHeight),
  ];
}
