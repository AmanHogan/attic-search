/** Types and fetch helpers for the attic API. */

export type DocType =
  | "receipt"
  | "estimate"
  | "statement"
  | "letter"
  | "certificate"
  | "record"
  | "schoolwork"
  | "id_card"
  | "form"
  | "program"
  | "note"
  | "photo"
  | "other";

export type AmountKind = "receipt" | "estimate" | "award";

export interface Amount {
  kind: AmountKind;
  party: string | null;
  date: string | null;
  amount: number | null;
  odometer: number | null;
  service: string | null;
  category: string | null;
}

/** A row in the documents list: the document plus its money and first page. */
export interface DocumentRow {
  doc_id: string;
  title: string | null;
  doc_type: DocType | null;
  date_start: string | null;
  date_end: string | null;
  summary: string | null;
  caption: string | null;
  sensitive: number;
  kind: AmountKind | null;
  party: string | null;
  amount: number | null;
  category: string | null;
  service: string | null;
  odometer: number | null;
  page_count: number;
  first_page_id: string | null;
  tags: string | null;
}

export interface Page {
  page_id: string;
  page_no: number;
  source_filename: string;
  engine: string | null;
  confidence: number | null;
  text: string;
  /** 0 for a text-native file (.docx, .doc), which has text but no page image. */
  has_image: number;
}

/** One document in full, as shown on its own page. */
export interface DocumentDetail {
  doc_id: string;
  title: string | null;
  doc_type: DocType | null;
  date_start: string | null;
  date_end: string | null;
  summary: string | null;
  caption: string | null;
  sensitive: number;
  created_at: string;
  amount: Amount | null;
  tags: string[];
  people: string[];
  pages: Page[];
}

export interface SearchHit {
  doc_id: string;
  page_id: string;
  page_no: number;
  title: string | null;
  doc_type: string | null;
  date_start: string | null;
  date_end: string | null;
  source_filename: string;
  /** null for a text-native file, which has no page image. */
  image_path: string | null;
  snippet: string;
  score: number;
  matched_by: string;
}

export interface Answer {
  question: string;
  route: "rag" | "sql";
  text: string;
  note: string | null;
  sql: string | null;
  rows: Record<string, unknown>[];
  sources: SearchHit[];
}

export interface Chat {
  chat_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

/** One turn in a chat. The answer fields are filled only on assistant messages. */
export interface ChatMessage {
  message_id: string;
  chat_id: string;
  role: "user" | "assistant";
  text: string;
  created_at: string;
  /** User only: the follow-up as rewritten for search, or null if used as written. */
  standalone: string | null;
  route: "rag" | "sql" | null;
  sql: string | null;
  note: string | null;
  sources: SearchHit[];
  rows: Record<string, unknown>[];
}

export interface ChatDetail {
  chat: Chat;
  messages: ChatMessage[];
}

export interface Stats {
  documents: number;
  pages: number;
  raw_files: number;
  jobs: { stage: string; status: string; n: number }[];
  doc_types: { doc_type: string; n: number }[];
  years: { year: string; n: number }[];
}

export interface Meta {
  doc_types: DocType[];
  categories: string[];
  services: string[];
  amount_kinds: AmountKind[];
}

/**
 * Call the API and parse its JSON, turning any error response into an Error.
 *
 * On the server there is no origin to resolve a relative URL against, so one
 * is taken from the environment; in the browser the Next.js rewrite handles it.
 */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const base = typeof window === "undefined" ? (process.env.ATTIC_API ?? "http://127.0.0.1:8000") : "";
  const response = await fetch(`${base}${path}`, {
    cache: "no-store",
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(detail?.detail ?? `${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

/** Format a document's date range for display. */
export function formatDates(start: string | null, end: string | null): string {
  if (!start) return "undated";
  return start === end || !end ? start : `${start} → ${end}`;
}

/** Format dollars, or an em dash when there is no amount. */
export function formatMoney(amount: number | null | undefined): string {
  if (amount === null || amount === undefined) return "—";
  return amount.toLocaleString("en-US", { style: "currency", currency: "USD" });
}
