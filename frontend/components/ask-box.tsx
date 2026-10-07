"use client";

import { useState } from "react";
import { AnswerDetails } from "@/components/answer-details";
import { api, type Answer } from "@/lib/api";

const EXAMPLES = [
  "What was the highest estimate I got for my car?",
  "How much scholarship money did UTA give me?",
  "When did I graduate high school?",
  "Do I have any records of medical shots?",
];

/** Question box: asks the API and shows the answer with its sources or SQL. */
export function AskBox() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function ask(q: string) {
    if (!q.trim() || busy) return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      setAnswer(
        await api<Answer>("/api/ask", {
          method: "POST",
          body: JSON.stringify({ question: q }),
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
        className="flex gap-2"
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. how much have I spent on my car?"
          className="flex-1 rounded-lg border border-line bg-card px-4 py-3 text-sm outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={busy || !question.trim()}
          className="rounded-lg bg-accent px-5 py-3 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>

      {!answer && !busy && (
        <div className="flex flex-wrap gap-2">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              onClick={() => {
                setQuestion(example);
                void ask(example);
              }}
              className="rounded-full border border-line px-3 py-1 text-xs text-muted hover:text-foreground"
            >
              {example}
            </button>
          ))}
        </div>
      )}

      {error && (
        <p className="rounded-lg border border-line bg-card p-4 text-sm">
          {error}
        </p>
      )}

      {answer && (
        <div className="space-y-4 rounded-lg border border-line bg-card p-5">
          <div className="flex items-start justify-between gap-4">
            <p className="text-[15px] leading-relaxed">{answer.text}</p>
            <span className="shrink-0 rounded-full border border-line px-2 py-0.5 text-[11px] uppercase tracking-wide text-muted">
              {answer.route}
            </span>
          </div>

          <AnswerDetails note={answer.note} sql={answer.sql} rows={answer.rows} sources={answer.sources} />
        </div>
      )}
    </div>
  );
}
