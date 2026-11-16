export function Working({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-center gap-3 rounded-xl bg-blue-50 p-3 font-medium text-blue-900" role="status">
      <span className="h-5 w-5 shrink-0 animate-spin rounded-full border-2 border-blue-300 border-t-blue-700" aria-hidden="true" />
      {children}
    </p>
  );
}

export function ErrorNote({ children }: { children: React.ReactNode }) {
  return <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm font-medium text-red-800">{children}</p>;
}
