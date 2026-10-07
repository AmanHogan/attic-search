import Link from "next/link";
import { PdfLink } from "@/components/pdf-link";
import { TextFileIcon } from "@/components/text-file-icon";
import { formatDates, type SearchHit } from "@/lib/api";

/** What an answer is based on: a note, the SQL it ran with its rows, and the cited pages. */
export function AnswerDetails({
  note,
  sql,
  rows,
  sources,
}: {
  note: string | null;
  sql: string | null;
  rows: Record<string, unknown>[];
  sources: SearchHit[];
}) {
  return (
    <>
      {note && <p className="text-xs text-muted">{note}</p>}

      {sql && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted">
            Query it ran ({rows.length} row{rows.length === 1 ? "" : "s"})
          </summary>
          <pre className="mt-2 overflow-x-auto rounded border border-line p-3 text-[11px] leading-relaxed">
            {sql}
          </pre>
          {rows.length > 0 && (
            <pre className="mt-2 overflow-x-auto rounded border border-line p-3 text-[11px]">
              {JSON.stringify(rows, null, 2)}
            </pre>
          )}
        </details>
      )}

      {sources.length > 0 && (
        <div className="space-y-3 border-t border-line pt-4">
          <p className="text-xs uppercase tracking-wide text-muted">Sources</p>
          <div className="grid gap-3 sm:grid-cols-2">
            {sources.map((hit, i) => (
              <div key={hit.page_id} className="rounded-lg border border-line p-3 hover:border-accent">
                <Link href={`/documents/${hit.doc_id}`} className="flex gap-3">
                  {hit.image_path ? (
                    /* eslint-disable-next-line @next/next/no-img-element */
                    <img
                      src={`/api/pages/${hit.page_id}/thumb`}
                      alt=""
                      className="h-20 w-16 shrink-0 rounded border border-line object-cover"
                    />
                  ) : (
                    <TextFileIcon className="h-20 w-16" />
                  )}
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      [{i + 1}] {hit.title ?? "Untitled"}
                    </p>
                    <p className="text-xs text-muted">
                      {hit.doc_type ?? "?"} · {formatDates(hit.date_start, hit.date_end)} · page {hit.page_no}
                    </p>
                    <p className="mt-1 line-clamp-2 text-xs text-muted">{hit.snippet}</p>
                  </div>
                </Link>
                <div className="mt-2">
                  <PdfLink pageId={hit.page_id} pageNo={hit.page_no} filename={hit.source_filename} />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}
