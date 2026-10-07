"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { AnswerDetails } from "@/components/answer-details";
import { api, type Chat, type ChatDetail, type ChatMessage } from "@/lib/api";

const EXAMPLES = [
  "When was my last oil change?",
  "How much scholarship money did UTA give me?",
  "Which classes did I take with Professor Van Vleet?",
  "Do I have any records of medical shots?",
];

/**
 * A conversation: its messages, and a box for the next question.
 *
 * With no `chatId` this is a new chat: the first question creates it on the server,
 * then the page moves to the chat's own URL. Later questions are follow-ups, which
 * the server rewrites to stand alone before answering.
 */
export function ChatThread({ chatId, initialMessages }: { chatId: string | null; initialMessages: ChatMessage[] }) {
  const router = useRouter();
  const [messages, setMessages] = useState(initialMessages);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || pending) return;
    setPending(question);
    setDraft("");
    setError(null);
    let id = chatId;
    try {
      id = id ?? (await api<Chat>("/api/chats", { method: "POST", body: "{}" })).chat_id;
      const turn = await api<{ user: ChatMessage; assistant: ChatMessage }>(`/api/chats/${id}/messages`, {
        method: "POST",
        body: JSON.stringify({ text: question }),
      });
      if (chatId) {
        setMessages((current) => [...current, turn.user, turn.assistant]);
        router.refresh(); // the sidebar's order and title
      } else {
        router.push(`/chat/${id}`);
        router.refresh();
      }
    } catch (e) {
      // The request can fail after the server has already saved the answer (e.g. a proxy
      // timeout), so reload the chat and show whatever it holds before reporting an error.
      const saved = id ? await api<ChatDetail>(`/api/chats/${id}`).catch(() => null) : null;
      if (saved && saved.messages.length > messages.length) {
        if (chatId) setMessages(saved.messages);
        else router.push(`/chat/${id}`);
        router.refresh();
      } else {
        setError(e instanceof Error ? e.message : String(e));
        setDraft(question);
      }
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="flex min-h-[70vh] flex-1 flex-col">
      <div className="flex-1 space-y-5">
        {messages.length === 0 && !pending && (
          <div className="space-y-3 pt-8">
            <h1 className="text-2xl font-semibold tracking-tight">Ask your archive</h1>
            <p className="text-sm text-muted">
              Ask a question, then follow up: “how much was it?”, “and the year before?” Each chat is saved
              in the sidebar.
            </p>
            <div className="flex flex-wrap gap-2 pt-2">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  onClick={() => void send(example)}
                  className="rounded-full border border-line px-3 py-1 text-xs text-muted hover:text-foreground"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) =>
          message.role === "user" ? (
            <div key={message.message_id} className="flex flex-col items-end gap-1">
              <p className="max-w-[80%] rounded-2xl bg-accent px-4 py-2 text-sm text-white">{message.text}</p>
              {message.standalone && (
                <p className="max-w-[80%] text-right text-[11px] text-muted">searched as: {message.standalone}</p>
              )}
            </div>
          ) : (
            <div key={message.message_id} className="space-y-4 rounded-lg border border-line bg-card p-5">
              <div className="flex items-start justify-between gap-4">
                <p className="text-[15px] leading-relaxed">{message.text}</p>
                {message.route && (
                  <span className="shrink-0 rounded-full border border-line px-2 py-0.5 text-[11px] uppercase tracking-wide text-muted">
                    {message.route}
                  </span>
                )}
              </div>
              <AnswerDetails note={message.note} sql={message.sql} rows={message.rows} sources={message.sources} />
            </div>
          ),
        )}

        {pending && (
          <>
            <div className="flex justify-end">
              <p className="max-w-[80%] rounded-2xl bg-accent px-4 py-2 text-sm text-white opacity-70">{pending}</p>
            </div>
            <p className="text-sm text-muted">Thinking…</p>
          </>
        )}

        {error && <p className="rounded-lg border border-line bg-card p-4 text-sm">{error}</p>}
        <div ref={bottom} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send(draft);
        }}
        className="sticky bottom-0 mt-6 flex gap-2 bg-background py-4"
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={messages.length ? "Ask a follow-up…" : "e.g. how much have I spent on my car?"}
          className="flex-1 rounded-lg border border-line bg-card px-4 py-3 text-sm outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={Boolean(pending) || !draft.trim()}
          className="rounded-lg bg-accent px-5 py-3 text-sm font-medium text-white disabled:opacity-40"
        >
          {pending ? "Thinking…" : "Send"}
        </button>
      </form>
    </div>
  );
}
