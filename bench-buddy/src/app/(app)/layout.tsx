import Link from "next/link";
import { BenchBuddyLogo } from "@/components/logo";
import { requireShop } from "@/lib/shop";

// Give AI work started from these pages (triage, guides, quotes) room to finish.
export const maxDuration = 300;

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const { shop } = await requireShop();
  return (
    <>
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur print:hidden">
        <div className="mx-auto flex w-full max-w-7xl items-center gap-2 px-3 py-2">
          <Link href="/board" className="mr-auto min-w-0">
            <BenchBuddyLogo className="text-base whitespace-nowrap" />
            <span className="block truncate text-xs text-slate-500">{shop.name}</span>
          </Link>
          <Link href="/board" className="btn btn-secondary btn-sm">Board</Link>
          <Link href="/settings" className="btn btn-secondary btn-sm">Settings</Link>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 p-3 sm:p-4">{children}</main>
    </>
  );
}
