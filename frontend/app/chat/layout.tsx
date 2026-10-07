import { ChatSidebar } from "@/components/chat-sidebar";
import { api, type Chat } from "@/lib/api";

export default async function ChatLayout({ children }: { children: React.ReactNode }) {
  let chats: Chat[] = [];
  let error: string | null = null;
  try {
    chats = await api<Chat[]>("/api/chats");
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  if (error) {
    return (
      <p className="rounded-lg border border-line bg-card p-4 text-sm">
        Can’t reach the API: {error}. Start it with <code>uv run attic serve</code>.
      </p>
    );
  }

  return (
    <div className="flex gap-8">
      <ChatSidebar chats={chats} />
      {children}
    </div>
  );
}
