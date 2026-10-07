import Link from "next/link";

import { TextFileIcon } from "@/components/text-file-icon";
import { api, formatDates, formatMoney, type DocumentRow, type Stats } from "@/lib/api";

interface Props {
  searchParams: Promise<{ type?: string; year?: string; sort?: string; has_amount?: string }>;
}

export default async function DocumentsPage({ searchParams }: Props) {
  const filters = await searchParams;
  const query = new URLSearchParams({ limit: "300", sort: filters.sort ?? "date" });
  if (filters.type) query.set("doc_type", filters.type);
  if (filters.year) query.set("year", filters.year);
  if (filters.has_amount) query.set("has_amount", filters.has_amount);

  const [{ documents, total }, stats] = await Promise.all([
    api<{ total: number; documents: DocumentRow[] }>(`/api/documents?${query}`),
    api<Stats>("/api/stats"),
  ]);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Documents</h1>
        <p className="text-sm text-muted">{total} shown</p>
      </div>

      <div className="space-y-2">
        <FilterRow label="Type" param="type" active={filters.type} options={stats.doc_types.map((t) => [t.doc_type, `${t.doc_type} (${t.n})`])} current={filters} />
        <FilterRow label="Year" param="year" active={filters.year} options={stats.years.map((y) => [y.year, `${y.year} (${y.n})`])} current={filters} />
        <FilterRow
          label="Sort"
          param="sort"
          active={filters.sort ?? "date"}
          options={[["date", "date"], ["title", "title"], ["amount", "amount"]]}
          current={filters}
          clearable={false}
        />
      </div>

      <div className="overflow-hidden rounded-lg border border-line bg-card">
        <table className="w-full text-sm">
          <thead className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="px-4 py-3 font-medium">Document</th>
              <th className="px-4 py-3 font-medium">Type</th>
              <th className="px-4 py-3 font-medium">Date</th>
              <th className="px-4 py-3 text-right font-medium">Amount</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((doc) => (
              <tr key={doc.doc_id} className="border-b border-line last:border-0 hover:bg-background/60">
                <td className="px-4 py-3">
                  <Link href={`/documents/${doc.doc_id}`} className="flex items-center gap-3">
                    {doc.first_page_id ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={`/api/pages/${doc.first_page_id}/thumb`}
                        alt=""
                        className="h-12 w-9 shrink-0 rounded border border-line object-cover"
                      />
                    ) : (
                      <TextFileIcon className="h-12 w-9" />
                    )}
                    <span className="min-w-0">
                      <span className="block truncate font-medium">{doc.title ?? "(not extracted yet)"}</span>
                      <span className="block truncate text-xs text-muted">
                        {doc.tags ?? doc.caption ?? ""}
                        {doc.page_count > 1 && ` · ${doc.page_count} pages`}
                      </span>
                    </span>
                  </Link>
                </td>
                <td className="px-4 py-3 text-muted">{doc.doc_type ?? "—"}</td>
                <td className="px-4 py-3 tabular-nums text-muted">{formatDates(doc.date_start, doc.date_end)}</td>
                <td className="px-4 py-3 text-right tabular-nums">
                  {doc.amount !== null && (
                    <>
                      <span className={doc.kind === "award" ? "text-accent" : ""}>{formatMoney(doc.amount)}</span>
                      <span className="block text-xs text-muted">{doc.kind}</span>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FilterRow({
  label,
  param,
  active,
  options,
  current,
  clearable = true,
}: {
  label: string;
  param: string;
  active?: string;
  options: [string, string][];
  current: Record<string, string | undefined>;
  clearable?: boolean;
}) {
  const link = (value?: string) => {
    const next = new URLSearchParams(
      Object.entries(current).filter(([, v]) => v) as [string, string][]
    );
    if (value) next.set(param, value);
    else next.delete(param);
    return `/documents?${next}`;
  };

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="w-10 text-muted">{label}</span>
      {clearable && (
        <Link
          href={link()}
          className={`rounded-full border px-3 py-1 ${!active ? "border-accent text-accent" : "border-line text-muted"}`}
        >
          all
        </Link>
      )}
      {options.map(([value, text]) => (
        <Link
          key={value}
          href={link(value)}
          className={`rounded-full border px-3 py-1 ${active === value ? "border-accent text-accent" : "border-line text-muted"}`}
        >
          {text}
        </Link>
      ))}
    </div>
  );
}
