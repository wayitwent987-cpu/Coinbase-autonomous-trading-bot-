import Link from "next/link";
import { redirect } from "next/navigation";
import { BenchBuddyLogo } from "@/components/logo";
import { getUser } from "@/lib/shop";

const FEATURES = [
  ["Intake by link or QR code", "Customers fill in their own details and photos. No more scribbled notes."],
  ["AI triage in seconds", "Top 3 likely causes, what to test first, and loud safety warnings."],
  ["Step-by-step repair guides", "A guide for the exact job, with what to double-check called out."],
  ["Quotes and final pricing", "Drafted from your rates and markup. You approve before anything is sent."],
  ["Automatic status emails", "Move a ticket and the customer is told. Fewer “is it done?” calls."],
] as const;

export default async function Home() {
  if (await getUser()) redirect("/board");
  return (
    <main className="mx-auto w-full max-w-2xl flex-1 space-y-8 p-4 py-10">
      <BenchBuddyLogo className="text-2xl" />
      <div className="space-y-3">
        <h1 className="text-4xl font-extrabold tracking-tight text-balance">The helper every repair bench needs.</h1>
        <p className="text-lg text-slate-600">
          For independent phone, computer and console repair shops. Set up in under 10 minutes.
        </p>
      </div>
      <div className="flex flex-col gap-3 sm:flex-row">
        <Link href="/signup" className="btn btn-primary flex-1 text-lg">Create your shop</Link>
        <Link href="/login" className="btn btn-secondary flex-1 text-lg">Log in</Link>
      </div>
      <ul className="space-y-3">
        {FEATURES.map(([title, text]) => (
          <li key={title} className="card">
            <p className="font-bold">{title}</p>
            <p className="text-slate-600">{text}</p>
          </li>
        ))}
      </ul>
    </main>
  );
}
