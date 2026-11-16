import Link from "next/link";
import { BenchBuddyLogo } from "@/components/logo";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center gap-6 p-4">
      <Link href="/" className="self-center text-xl"><BenchBuddyLogo /></Link>
      {children}
    </main>
  );
}
