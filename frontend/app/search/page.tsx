import Link from "next/link";
import { PdfLink } from "@/components/pdf-link";
import { TextFileIcon } from "@/components/text-file-icon";
import { api, formatDates, type SearchHit } from "@/lib/api";

interface Props {
  searchParams: Promise<{ q?: string }>;
}

export default async function SearchPage({ searchParams }: Props) {
  const { q } = await searchParams;
  const hits = q
    ? (
        await api<{ hits: SearchHit[] }>(
          `/api/search?q=${encodeURIComponent(q)}`,
        )
      ).hits
    : [];

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Search</h1>
      <p className="-mt-4 text-sm text-muted">
        Matches both exact words and meaning, so “contacts” finds a page that
        says “contact lens”.
      </p>

      <form className="flex gap-2">
        <input
          name="q"
          defaultValue={q ?? ""}
          placeholder="oil change, scholarship, grandma…"
          className="flex-1 rounded-lg border border-line bg-card px-4 py-3 text-sm outline-none focus:border-accent"
        />
        <button className="rounded-lg bg-accent px-5 py-3 text-sm font-medium text-white">
          Search
        </button>
      </form>

      {q && hits.length === 0 && (
        <p className="text-sm text-muted">No matches.</p>
      )}

      <div className="space-y-3">
        {hits.map((hit) => (
          <div
            key={hit.page_id}
            className="rounded-lg border border-line bg-card p-4 hover:border-accent"
          >
            <Link href={`/documents/${hit.doc_id}`} className="flex gap-4">
              {hit.image_path ? (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img
                  src={`/api/pages/${hit.page_id}/thumb`}
                  alt=""
                  className="h-24 w-20 shrink-0 rounded border border-line object-cover"
                />
              ) : (
                <TextFileIcon className="h-24 w-20" />
              )}
              <div className="min-w-0">
                <p className="font-medium">{hit.title ?? "Untitled"}</p>
                <p className="text-xs text-muted">
                  {hit.doc_type ?? "?"} ·{" "}
                  {formatDates(hit.date_start, hit.date_end)} · page{" "}
                  {hit.page_no} · matched by {hit.matched_by}
                </p>
                <p className="mt-2 line-clamp-3 text-sm text-muted">
                  {hit.snippet}
                </p>
              </div>
            </Link>
            <div className="mt-2">
              <PdfLink
                pageId={hit.page_id}
                pageNo={hit.page_no}
                filename={hit.source_filename}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
