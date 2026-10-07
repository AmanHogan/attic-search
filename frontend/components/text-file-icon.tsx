/** Stands in for a page thumbnail when a document is a text file with no image. */
export function TextFileIcon({ className = "" }: { className?: string }) {
  return (
    <span
      className={`flex shrink-0 items-center justify-center rounded border border-line bg-card text-muted ${className}`}
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
        className="h-1/2 w-1/2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      >
        <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
        <path d="M14 3v5h5M8.5 13h7M8.5 16.5h7" />
      </svg>
    </span>
  );
}
