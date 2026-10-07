"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, formatMoney, type Amount, type DocumentDetail, type Meta } from "@/lib/api";

/**
 * Shows a document's extracted facts and lets you correct them.
 *
 * Extraction is a local model's best guess, so every field here is editable;
 * saving also re-queues the document for indexing so search stays in step.
 */
export function DocumentEditor({ document, meta }: { document: DocumentDetail; meta: Meta }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    title: document.title ?? "",
    doc_type: document.doc_type ?? "",
    date_start: document.date_start ?? "",
    date_end: document.date_end ?? "",
    summary: document.summary ?? "",
    caption: document.caption ?? "",
    sensitive: Boolean(document.sensitive),
    tags: document.tags.join(", "),
    people: document.people.join(", "),
  });
  const [money, setMoney] = useState<Amount>(
    document.amount ?? {
      kind: "receipt",
      party: null,
      date: null,
      amount: null,
      odometer: null,
      service: null,
      category: null,
    }
  );
  const [hasMoney, setHasMoney] = useState(document.amount !== null);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api(`/api/documents/${document.doc_id}`, {
        method: "PATCH",
        body: JSON.stringify({
          title: form.title || null,
          doc_type: form.doc_type || null,
          date_start: form.date_start || null,
          date_end: form.date_end || form.date_start || null,
          summary: form.summary || null,
          caption: form.caption || null,
          sensitive: form.sensitive,
          tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
          people: form.people.split(",").map((p) => p.trim()).filter(Boolean),
        }),
      });
      if (hasMoney) {
        await api(`/api/documents/${document.doc_id}/amount`, {
          method: "PUT",
          body: JSON.stringify({ ...money, amount: money.amount === null ? null : Number(money.amount) }),
        });
      } else if (document.amount) {
        await api(`/api/documents/${document.doc_id}/amount`, { method: "DELETE" });
      }
      setEditing(false);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    const label = document.title ?? "this document";
    if (!window.confirm(`Delete "${label}" from the archive? The original file is kept on disk.`)) return;
    setError(null);
    try {
      await api(`/api/documents/${document.doc_id}`, { method: "DELETE" });
      router.push("/documents");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!editing) {
    return (
      <aside className="space-y-4 rounded-lg border border-line bg-card p-5">
        <div className="flex items-start justify-between gap-3">
          <h1 className="text-lg font-semibold leading-tight">{document.title ?? "(not extracted yet)"}</h1>
          <button
            onClick={() => setEditing(true)}
            className="shrink-0 rounded-full border border-line px-3 py-1 text-xs text-muted hover:text-foreground"
          >
            Edit
          </button>
        </div>

        <dl className="space-y-2 text-sm">
          <Row label="Type" value={document.doc_type} />
          <Row
            label="Written"
            value={
              document.date_start
                ? document.date_start === document.date_end
                  ? document.date_start
                  : `${document.date_start} → ${document.date_end}`
                : null
            }
          />
          {document.amount && (
            <>
              <Row label={document.amount.kind} value={formatMoney(document.amount.amount)} />
              <Row label="Party" value={document.amount.party} />
              <Row label="Category" value={document.amount.category} />
              <Row label="Service" value={document.amount.service} />
              <Row label="Odometer" value={document.amount.odometer?.toLocaleString()} />
            </>
          )}
          <Row label="Tags" value={document.tags.join(", ")} />
          <Row label="People" value={document.people.join(", ")} />
          {document.sensitive ? <Row label="Sensitive" value="yes" /> : null}
        </dl>

        {document.summary && <p className="border-t border-line pt-3 text-sm leading-relaxed">{document.summary}</p>}
        {document.caption && <p className="text-sm italic leading-relaxed text-muted">{document.caption}</p>}

        <div className="border-t border-line pt-3">
          <button onClick={remove} className="text-xs text-red-600 hover:underline">
            Delete document
          </button>
          {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
        </div>
      </aside>
    );
  }

  return (
    <aside className="space-y-4 rounded-lg border border-accent bg-card p-5">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Correct this document</h2>

      <Field label="Title">
        <input className={input} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
      </Field>

      <Field label="Type">
        <select
          className={input}
          value={form.doc_type}
          onChange={(e) => setForm({ ...form, doc_type: e.target.value })}
        >
          <option value="">(none)</option>
          {meta.doc_types.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </Field>

      <div className="grid grid-cols-2 gap-3">
        <Field label="Date from">
          <input
            type="date"
            className={input}
            value={form.date_start}
            onChange={(e) => setForm({ ...form, date_start: e.target.value })}
          />
        </Field>
        <Field label="Date to">
          <input
            type="date"
            className={input}
            value={form.date_end}
            onChange={(e) => setForm({ ...form, date_end: e.target.value })}
          />
        </Field>
      </div>

      <Field label="Tags (comma separated)">
        <input className={input} value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
      </Field>

      <Field label="People (comma separated)">
        <input className={input} value={form.people} onChange={(e) => setForm({ ...form, people: e.target.value })} />
      </Field>

      <Field label="Summary">
        <textarea
          rows={3}
          className={input}
          value={form.summary}
          onChange={(e) => setForm({ ...form, summary: e.target.value })}
        />
      </Field>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={form.sensitive}
          onChange={(e) => setForm({ ...form, sensitive: e.target.checked })}
        />
        Sensitive (kept out of answers by default)
      </label>

      <div className="space-y-3 border-t border-line pt-4">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input type="checkbox" checked={hasMoney} onChange={(e) => setHasMoney(e.target.checked)} />
          This document involves money
        </label>

        {hasMoney && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Kind">
                <select
                  className={input}
                  value={money.kind}
                  onChange={(e) => setMoney({ ...money, kind: e.target.value as Amount["kind"] })}
                >
                  {meta.amount_kinds.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Amount ($)">
                <input
                  type="number"
                  step="0.01"
                  className={input}
                  value={money.amount ?? ""}
                  onChange={(e) =>
                    setMoney({ ...money, amount: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </Field>
            </div>

            <Field label="Party (who was paid, or who paid you)">
              <input
                className={input}
                value={money.party ?? ""}
                onChange={(e) => setMoney({ ...money, party: e.target.value || null })}
              />
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Category">
                <select
                  className={input}
                  value={money.category ?? ""}
                  onChange={(e) => setMoney({ ...money, category: e.target.value || null })}
                >
                  <option value="">(none)</option>
                  {meta.categories.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Date">
                <input
                  type="date"
                  className={input}
                  value={money.date ?? ""}
                  onChange={(e) => setMoney({ ...money, date: e.target.value || null })}
                />
              </Field>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Vehicle service">
                <select
                  className={input}
                  value={money.service ?? ""}
                  onChange={(e) => setMoney({ ...money, service: e.target.value || null })}
                >
                  <option value="">(none)</option>
                  {meta.services.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Odometer">
                <input
                  type="number"
                  className={input}
                  value={money.odometer ?? ""}
                  onChange={(e) =>
                    setMoney({ ...money, odometer: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </Field>
            </div>
          </>
        )}
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="flex gap-2 border-t border-line pt-4">
        <button
          onClick={() => void save()}
          disabled={saving}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          onClick={() => setEditing(false)}
          className="rounded-lg border border-line px-4 py-2 text-sm text-muted"
        >
          Cancel
        </button>
      </div>
    </aside>
  );
}

const input =
  "w-full rounded border border-line bg-background px-3 py-2 text-sm outline-none focus:border-accent";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs uppercase tracking-wide text-muted">{label}</span>
      {children}
    </label>
  );
}

function Row({ label, value }: { label: string; value: string | number | null | undefined }) {
  if (!value) return null;
  return (
    <div className="flex gap-3">
      <dt className="w-24 shrink-0 text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="min-w-0 flex-1">{value}</dd>
    </div>
  );
}
