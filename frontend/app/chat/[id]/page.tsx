import { notFound } from "next/navigation";
import { ChatThread } from "@/components/chat-thread";
import { api, type ChatDetail } from "@/lib/api";

interface Props {
  params: Promise<{ id: string }>;
}

export default async function ChatPage({ params }: Props) {
  const { id } = await params;
  let detail: ChatDetail | null = null;
  try {
    detail = await api<ChatDetail>(`/api/chats/${encodeURIComponent(id)}`);
  } catch {
    detail = null;
  }
  // notFound() works by throwing, so it must stay outside the try above.
  if (!detail) notFound();

  // Keyed by chat, so switching chats starts a fresh thread instead of reusing state.
  return <ChatThread key={id} chatId={id} initialMessages={detail.messages} />;
}
