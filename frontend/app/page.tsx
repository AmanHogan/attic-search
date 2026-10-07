import { AskBox } from "@/components/ask-box";
import { api, type Stats } from "@/lib/api";

export default async function AskPage() {
  let stats: Stats | null = null;
  let error: string | null = null;
  try {
    stats = await api<Stats>("/api/stats");
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  const pending = stats?.jobs.filter((job) => job.status === "pending") ?? [];

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Ask your archive</h1>
        <p className="mt-1 text-sm text-muted">
          Questions about what a document says are answered from the pages themselves. Totals, counts
          and “when did I last…” are answered by querying the extracted facts.
        </p>
      </div>

      {error ? (
        <p className="rounded-lg border border-line bg-card p-4 text-sm">
          Can’t reach the API: {error}. Start it with <code>uv run attic serve</code>.
        </p>
      ) : (
        <>
          <AskBox />
          {stats && (
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Documents" value={stats.documents} />
              <Stat label="Pages" value={stats.pages} />
              <Stat label="Files" value={stats.raw_files} />
              <Stat
                label="Pending work"
                value={pending.reduce((n, job) => n + job.n, 0)}
                hint={pending.map((job) => `${job.n} ${job.stage}`).join(", ")}
              />
            </dl>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <div className="rounded-lg border border-line bg-card px-4 py-3">
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums">{value}</dd>
      {hint && <dd className="text-xs text-muted">{hint}</dd>}
    </div>
  );
}
