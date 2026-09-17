import { useEffect, useRef, useState } from 'react';
import { Canvas, FabricImage } from 'fabric';
import { computeContainFit } from './geometry';

/**
 * Shared Fabric canvas + document-image setup, extracted from the
 * Template Designer (Module 3) so the Review UI's highlight view can reuse
 * the exact same fit-to-contain/letterboxing behavior instead of a second,
 * possibly-drifting implementation. Callers attach `canvasElRef` to a
 * <canvas> element and get back the live Fabric canvas/image once loaded -
 * what they draw on top (interactive drawing vs. a static highlight box) is
 * left entirely to them.
 */
export function useDocumentCanvas(imageUrl, canvasW, canvasH) {
  const canvasElRef = useRef(null);
  const fabricCanvasRef = useRef(null);
  const imageRef = useRef(null);
  const [imageReady, setImageReady] = useState(false);

  useEffect(() => {
    const canvas = new Canvas(canvasElRef.current, {
      width: canvasW,
      height: canvasH,
      backgroundColor: '#f4f4f4',
      selection: true,
    });
    fabricCanvasRef.current = canvas;
    setImageReady(false);

    let cancelled = false;
    FabricImage.fromURL(imageUrl).then((img) => {
      if (cancelled) return;
      const fit = computeContainFit(img.width, img.height, canvasW, canvasH);
      img.set({
        left: fit.left,
        top: fit.top,
        scaleX: fit.scale,
        scaleY: fit.scale,
        originX: 'left',
        originY: 'top',
        selectable: false,
        evented: false,
      });
      canvas.add(img); // added first -> renders behind anything drawn after it
      imageRef.current = img;
      canvas.renderAll();
      setImageReady(true);
    });

    return () => {
      cancelled = true;
      canvas.dispose();
    };
  }, [imageUrl, canvasW, canvasH]);

  return { canvasElRef, fabricCanvasRef, imageRef, imageReady };
}
