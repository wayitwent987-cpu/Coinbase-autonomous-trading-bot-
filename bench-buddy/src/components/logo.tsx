export function BenchBuddyLogo({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 font-bold tracking-tight ${className}`}>
      <svg viewBox="0 0 32 32" className="h-7 w-7" aria-hidden="true">
        <rect width="32" height="32" rx="8" fill="#1d4ed8" />
        <path d="M21.5 8.5a5 5 0 0 0-6.4 6.4L8.6 21.4a1.6 1.6 0 0 0 2.3 2.3l6.5-6.5a5 5 0 0 0 6.4-6.4l-2.9 2.9-2.3-.6-.6-2.3z" fill="#fff" />
      </svg>
      Bench Buddy
    </span>
  );
}
