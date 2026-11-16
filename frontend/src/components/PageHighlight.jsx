import { useEffect, useRef, useState } from 'react';
import { locateResultField, resultImageUrl } from '@/api';
import { cn } from '@/lib/utils';

const OUTLINE = { auto: 'border-success-solid', spot_check: 'border-warning-solid', review: 'border-danger-solid' };

/**
 * The whole stored page with the words a value was read from marked on it. For a page read without a template there is no field
 * box to crop to, so the reviewer sees the full page and the printed text the value came from. The boxes come from
 * GET /results/{id}/locate (a Tesseract pass over the stored page, display only); no match simply shows the page.
 */
export function PageHighlight({ resultId, fieldId, tier, sourceText }) {
  const [boxes, setBoxes] = useState(null);
  const [failed, setFailed] = useState(false);
  const firstRef = useRef(null);

  useEffect(() => {
    let live = true;
    setBoxes(null);
    locateResultField(resultId, fieldId)
      .then((out) => live && setBoxes(out.boxes ?? []))
      .catch(() => live && setBoxes([]));
    return () => {
      live = false;
    };
  }, [resultId, fieldId]);

  useEffect(() => {
    firstRef.current?.scrollIntoView({ block: 'center', behavior: 'auto' });
  }, [boxes]);

  return (
    <div className="flex flex-col gap-2">
      <div className="scroll-thin relative max-h-[70vh] overflow-auto rounded-md border border-border bg-white" data-testid="page-highlight">
        {failed ? (
          <div className="grid h-32 place-items-center text-xs text-foreground-subtle">No page image was saved for this document</div>
        ) : (
          <div className="relative">
            <img src={resultImageUrl(resultId)} alt={`The page field ${fieldId} was read from`} className="block w-full" onError={() => setFailed(true)} />
            {(boxes ?? []).map(([x0, y0, x1, y1], i) => (
              <span
                key={i}
                ref={i === 0 ? firstRef : undefined}
                data-testid="source-box"
                className={cn('pointer-events-none absolute rounded-sm border-2 bg-warning-solid/20', OUTLINE[tier] ?? 'border-foreground')}
                style={{ left: `${x0 * 100 - 0.3}%`, top: `${y0 * 100 - 0.2}%`, width: `${(x1 - x0) * 100 + 0.6}%`, height: `${(y1 - y0) * 100 + 0.4}%` }}
              />
            ))}
          </div>
        )}
      </div>
      <p className="text-xs text-foreground-secondary">
        {boxes === null
          ? 'Finding the source text on the page…'
          : boxes.length
            ? 'The marked words are the printed text this value was read from.'
            : sourceText
              ? 'The source text could not be pinpointed on the page; the whole page is shown.'
              : 'Nothing was read for this field; the whole page is shown.'}
      </p>
      {sourceText ? (
        <p className="break-words rounded-sm border border-border bg-surface px-3 py-2 font-mono text-sm text-foreground" data-testid="source-text">
          {sourceText}
        </p>
      ) : null}
    </div>
  );
}
