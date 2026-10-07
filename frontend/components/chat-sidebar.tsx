"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { api, type Chat } from "@/lib/api";

/** List of chats: open one, start a new one, or delete one. */
export function ChatSidebar({ chats }: { chats: Chat[] }) {
  const pathname = usePathname();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  async function remove(chat: Chat) {
    if (!window.confirm(`Delete “${chat.title}”? Its messages are removed; your documents are not.`)) return;
    setError(null);
    try {
      await api(`/api/chats/${chat.chat_id}`, { method: "DELETE" });
      if (pathname === `/chat/${chat.chat_id}`) router.push("/chat");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <aside className="flex w-60 shrink-0 flex-col gap-3">
      <Link
        href="/chat"
        className="rounded-lg bg-accent px-4 py-2 text-center text-sm font-medium text-white"
      >
        New chat
      </Link>
      {error && <p className="text-xs text-red-600">{error}</p>}
      {chats.length === 0 ? (
        <p className="px-1 text-xs text-muted">No chats yet.</p>
      ) : (
        <ul className="space-y-1">
          {chats.map((chat) => {
            const active = pathname === `/chat/${chat.chat_id}`;
            return (
              <li
                key={chat.chat_id}
                className={`group flex items-center gap-1 rounded-lg px-2 py-1.5 ${
                  active ? "bg-card ring-1 ring-line" : "hover:bg-card"
                }`}
              >
                <Link
                  href={`/chat/${chat.chat_id}`}
                  title={chat.title}
                  className={`min-w-0 flex-1 truncate text-sm ${active ? "font-medium" : "text-muted"}`}
                >
                  {chat.title}
                </Link>
                <button
                  onClick={() => void remove(chat)}
                  aria-label={`Delete ${chat.title}`}
                  className="shrink-0 rounded px-1 text-xs text-muted opacity-0 hover:text-red-600 focus:opacity-100 group-hover:opacity-100"
                >
                  ✕
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </aside>
  );
}
