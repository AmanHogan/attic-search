"use client";

import { useState } from "react";
import type { Page } from "@/lib/api";

/** Shown in place of a page image for a text-native file, which has none. */
function TextOnlyNotice({ filename }: { filename: string }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-line bg-card px-4 py-3">
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
        className="h-8 w-8 shrink-0 text-muted"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      >
        <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
        <path d="M14 3v5h5M8.5 13h7M8.5 16.5h7" />
      </svg>
      <p className="text-xs text-muted">
        <span className="text-foreground">{filename}</span> is a text file, so it has no page image.
      </p>
    </div>
  );
}

/** Page image beside its text, with thumbnails when a document has several pages. */
export function PageViewer({ pages }: { pages: Page[] }) {
  const [index, setIndex] = useState(0);
  const [showText, setShowText] = useState(false);
  const page = pages[index];

  if (!page) return <p className="text-sm text-muted">This document has no pages.</p>;

  // A text-native file has nothing to show but its text, so the toggle is pointless.
  const hasImage = Boolean(page.has_image);
  const text = !hasImage || showText;
  const thumbs = pages.filter((p) => p.has_image);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-4">
        <p className="text-xs text-muted">
          Page {page.page_no} of {pages.length} · {page.source_filename}
          {page.confidence !== null && hasImage && ` · OCR ${(page.confidence * 100).toFixed(0)}%`}
        </p>
        {hasImage && (
          <button
            onClick={() => setShowText((v) => !v)}
            className="rounded-full border border-line px-3 py-1 text-xs text-muted hover:text-foreground"
          >
            {showText ? "Show image" : "Show text"}
          </button>
        )}
      </div>

      {!hasImage && page.source_filename.toLowerCase().endsWith(".pdf") ? (
        <a
          href={`/api/pages/${page.page_id}/pdf`}
          target="_blank"
          rel="noreferrer"
          className="inline-block rounded-full border border-line px-3 py-1 text-xs text-muted hover:text-foreground"
        >
          Open original PDF at page {page.page_no}
        </a>
      ) : (
        !hasImage && <TextOnlyNotice filename={page.source_filename} />
      )}

      {text ? (
        <pre className="max-h-[70vh] overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-card p-4 text-xs leading-relaxed">
          {page.text || "(no text)"}
        </pre>
      ) : (
        <a href={`/api/pages/${page.page_id}/image`} target="_blank" rel="noreferrer">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={`/api/pages/${page.page_id}/image`}
            alt={`Page ${page.page_no}`}
            className="max-h-[70vh] w-full rounded-lg border border-line bg-card object-contain"
          />
        </a>
      )}

      {pages.length > 1 && thumbs.length > 0 && (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {pages.map((p, i) => (
            <button key={p.page_id} onClick={() => setIndex(i)} className="shrink-0">
              {p.has_image ? (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img
                  src={`/api/pages/${p.page_id}/thumb`}
                  alt={`Page ${p.page_no}`}
                  className={`h-20 w-16 rounded border object-cover ${
                    i === index ? "border-accent" : "border-line opacity-70"
                  }`}
                />
              ) : (
                <span
                  className={`flex h-20 w-16 items-center justify-center rounded border text-xs ${
                    i === index ? "border-accent text-foreground" : "border-line text-muted"
                  }`}
                >
                  {p.page_no}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
