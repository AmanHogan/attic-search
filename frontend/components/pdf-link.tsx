/** Opens the original PDF at a page in a new tab; renders nothing for other file types. */
export function PdfLink({
  pageId,
  pageNo,
  filename,
}: {
  pageId: string;
  pageNo: number;
  filename: string;
}) {
  if (!filename.toLowerCase().endsWith(".pdf")) return null;
  return (
    <a
      href={`/api/pages/${pageId}/pdf`}
      target="_blank"
      rel="noreferrer"
      className="text-xs text-accent hover:underline"
    >
      Open PDF at page {pageNo}
    </a>
  );
}
