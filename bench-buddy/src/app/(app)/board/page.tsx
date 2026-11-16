import type { Metadata } from "next";
import Link from "next/link";
import { moveTicket } from "@/app/actions/tickets";
import { SafetyBadges } from "@/components/safety-banner";
import { SubmitButton } from "@/components/submit-button";
import { timeAgo } from "@/lib/format";
import { mergeFlags } from "@/lib/safety";
import { requireShop } from "@/lib/shop";
import { createClient } from "@/lib/supabase/server";
import { isStatus, nextStatus, STATUSES, STATUS_LABEL, type SafetyFlag, type TicketStatus } from "@/lib/types";

export const metadata: Metadata = { title: "Board" };

type Card = {
  id: string;
  number: number;
  customer_name: string;
  device_model: string;
  status: TicketStatus;
  created_at: string;
  safety_flags: SafetyFlag[];
  ai_flags: SafetyFlag[] | null;
};

export default async function BoardPage({ searchParams }: PageProps<"/board">) {
  const { shop } = await requireShop();
  const query = await searchParams;
  // On a phone you see one column at a time; this is the one that's open.
  const open: TicketStatus = isStatus(query.s) ? query.s : "received";

  const supabase = await createClient();
  const { data } = await supabase
    .from("tickets")
    .select("id, number, customer_name, device_model, status, created_at, safety_flags, ai_flags:triage->safety_flags")
    .order("created_at", { ascending: false })
    .limit(300);
  const tickets = (data ?? []) as unknown as Card[];
  const byStatus = (status: TicketStatus) => {
    const list = tickets.filter((t) => t.status === status);
    // Keep "Picked Up" short so the board stays fast.
    return status === "picked_up" ? list.slice(0, 20) : list;
  };

  return (
    <div className="space-y-3">
      {query.welcome === "1" && (
        <div className="card border-blue-200 bg-blue-50">
          <p className="font-bold">Your shop is ready! 🎉</p>
          <p>Tap <strong>New ticket</strong> to try the customer form yourself, or grab your QR code in Settings.</p>
        </div>
      )}
      <div className="flex items-center gap-2">
        <h1 className="mr-auto text-2xl font-bold">Tickets</h1>
        <Link href={`/intake/${shop.slug}`} target="_blank" className="btn btn-primary">+ New ticket</Link>
      </div>

      {/* Phone: tabs to switch column. */}
      <nav className="-mx-3 flex gap-2 overflow-x-auto px-3 pb-1 md:hidden" aria-label="Columns">
        {STATUSES.map((status) => (
          <Link
            key={status}
            href={`/board?s=${status}`}
            aria-current={status === open ? "page" : undefined}
            className={`btn btn-sm shrink-0 ${status === open ? "btn-primary" : "btn-secondary"}`}
          >
            {STATUS_LABEL[status]} <span className="rounded-full bg-black/10 px-2">{byStatus(status).length}</span>
          </Link>
        ))}
      </nav>

      {/* Bigger screens: all five columns side by side. */}
      <div className="md:grid md:grid-cols-5 md:gap-3">
        {STATUSES.map((status) => {
          const list = byStatus(status);
          const next = nextStatus(status);
          return (
            <section key={status} className={`${status === open ? "block" : "hidden"} md:block`} aria-label={STATUS_LABEL[status]}>
              <h2 className="mb-2 hidden items-center justify-between rounded-xl bg-slate-200 px-3 py-2 text-sm font-bold md:flex">
                {STATUS_LABEL[status]} <span className="rounded-full bg-white px-2">{list.length}</span>
              </h2>
              <ul className="space-y-2">
                {list.length === 0 && <li className="rounded-xl border border-dashed border-slate-300 p-4 text-center text-sm text-slate-500">Nothing here</li>}
                {list.map((ticket) => (
                  <li key={ticket.id} className="rounded-2xl border border-slate-200 bg-white shadow-sm">
                    <Link href={`/tickets/${ticket.id}`} className="block space-y-1 p-3">
                      <p className="flex items-baseline justify-between gap-2">
                        <span className="font-mono text-sm font-bold text-slate-500">#{ticket.number}</span>
                        <span className="text-xs text-slate-500">{timeAgo(ticket.created_at)}</span>
                      </p>
                      <p className="text-lg leading-tight font-bold">{ticket.device_model}</p>
                      <p className="text-sm text-slate-600">{ticket.customer_name}</p>
                      <SafetyBadges flags={mergeFlags(ticket.safety_flags, ticket.ai_flags ?? undefined)} />
                    </Link>
                    {next && (
                      <form action={moveTicket.bind(null, ticket.id)} className="border-t border-slate-100 p-2">
                        <input type="hidden" name="status" value={next} />
                        <SubmitButton className="btn btn-secondary btn-sm w-full" pendingText="Moving…">
                          Move to {STATUS_LABEL[next]} →
                        </SubmitButton>
                      </form>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
      </div>
      <p className="text-center text-xs text-slate-500">Moving a ticket emails the customer automatically.</p>
    </div>
  );
}
