import { useEffect, useRef } from "react";
import type { Source } from "../api/contract";
import { Markdown, safeUrl } from "./Markdown";

export interface BriefDoc {
  title: string;
  summary: string | null;
  markdown: string;
  sources: Source[];
  meta?: string;
}

/** Modal reader for a delivered brief. Native <dialog> gives focus trapping and Escape-to-close. */
export function BriefReader({ brief, onClose }: { brief: BriefDoc | null; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (brief && !dialog.open) dialog.showModal();
    if (!brief && dialog.open) dialog.close();
  }, [brief]);

  return (
    <dialog ref={ref} className="brief" aria-labelledby="brief-title" onClose={onClose}>
      {brief && (
        <article>
          <header className="brief__head">
            <div>
              <p className="eyebrow">Delivered brief</p>
              <h2 id="brief-title">{brief.title}</h2>
              {brief.meta && <p className="mono faint small">{brief.meta}</p>}
            </div>
            <button type="button" className="btn btn--ghost" onClick={onClose} autoFocus>
              Close
            </button>
          </header>
          {brief.summary && <p className="brief__summary">{brief.summary}</p>}
          <Markdown source={brief.markdown} />
          <section aria-labelledby="brief-sources" className="brief__sources">
            <h3 id="brief-sources">Sources</h3>
            {brief.sources.length === 0 ? (
              <p className="faint">No sources were attached.</p>
            ) : (
              <ol>
                {brief.sources.map((s, i) => {
                  const href = safeUrl(s.url);
                  return (
                    <li key={`${s.url}-${i}`}>
                      {href ? (
                        <a href={href} target="_blank" rel="noopener noreferrer">
                          {s.title || s.url}
                        </a>
                      ) : (
                        <span>{s.title || s.url}</span>
                      )}
                      <span className="mono faint small"> {s.url}</span>
                    </li>
                  );
                })}
              </ol>
            )}
          </section>
        </article>
      )}
    </dialog>
  );
}
