// Saving a ticket with photos can take a few seconds; AI triage then runs
// in the background after the customer has already seen "thanks".
export const maxDuration = 120;

export default function IntakeLayout({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto w-full max-w-xl flex-1 space-y-5 p-4 py-6">{children}</main>;
}
