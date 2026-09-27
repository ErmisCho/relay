import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

/** Only http(s) and mailto links survive; everything else (javascript:, data:, ...) is dropped. */
export function safeUrl(url: string): string | null {
  const cleaned = defaultUrlTransform(url);
  if (!cleaned) return null;
  try {
    const parsed = new URL(cleaned, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(parsed.protocol) ? cleaned : null;
  } catch {
    return null;
  }
}

/**
 * Renders an executor-written brief. Raw HTML in the Markdown is not rendered
 * (`skipHtml`), images are not loaded (no tracking pixels from fetched pages),
 * and links open in a new tab without referrer.
 */
export function Markdown({ source }: { source: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => safeUrl(url) ?? ""}
        components={{
          a: ({ href, children }) =>
            href ? (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ alt }) => <span className="md-img">[image: {alt || "untitled"}]</span>,
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
