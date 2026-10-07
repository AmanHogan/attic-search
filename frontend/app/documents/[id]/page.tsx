import Link from "next/link";
import { notFound } from "next/navigation";
import { DocumentEditor } from "@/components/document-editor";
import { PageViewer } from "@/components/page-viewer";
import { api, type DocumentDetail, type Meta } from "@/lib/api";

export default async function DocumentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let document: DocumentDetail;
  try {
    [document] = await Promise.all([api<DocumentDetail>(`/api/documents/${id}`)]);
  } catch {
    notFound();
  }
  const meta = await api<Meta>("/api/meta");

  return (
    <div className="space-y-6">
      <Link href="/documents" className="text-sm text-muted hover:text-foreground">
        ← Documents
      </Link>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <PageViewer pages={document.pages} />
        <DocumentEditor document={document} meta={meta} />
      </div>
    </div>
  );
}
