// Public "where's my repair?" page. Reached by a long random link that only
// the customer has. Shows status and approved prices - nothing else.
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { formatMoney } from "@/lib/quote-math";
import { logoUrl } from "@/lib/shop";
import { createAdminClient } from "@/lib/supabase/admin";
import { STATUSES, STATUS_LABEL, type TicketStatus } from "@/lib/types";

export const metadata: Metadata = { title: "Repair status", robots: { index: false } };

const CUSTOMER_STATUS_TEXT: Record<TicketStatus, string> = {
  received: "We've got your request and will take a look soon.",
  diagnosing: "Your device is on the bench and we're working out what's wrong.",
  waiting_parts: "We know what's needed and we're waiting on parts.",
  ready: "Your device is ready to pick up!",
  picked_up: "Picked up. Thanks for coming to us!",
};

export default async function StatusPage({ params }: PageProps<"/t/[token]">) {
  const { token } = await params;
  if (!/^[0-9a-f]{32}$/.test(token)) notFound();
  const admin = createAdminClient();
  const { data: ticket } = await admin
    .from("tickets")
    .select("id, number, device_model, status, updated_at, shops(name, phone, logo_path)")
    .eq("public_token", token)
    .maybeSingle();
  if (!ticket) notFound();
  const shop = ticket.shops as unknown as { name: string; phone: string | null; logo_path: string | null };
  const status = ticket.status as TicketStatus;

  const { data: quotes } = await admin.from("quotes").select("kind, total_cents").eq("ticket_id", ticket.id).neq("status", "draft");
  const price = quotes?.find((q) => q.kind === "final") ?? quotes?.find((q) => q.kind === "quote");
  const logo = logoUrl(shop);
  const current = STATUSES.indexOf(status);

  return (
    <main className="mx-auto w-full max-w-xl flex-1 space-y-5 p-4 py-6">
      <div className="flex items-center gap-3">
        {logo && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={logo} alt="" className="h-14 w-14 rounded-xl border border-slate-200 bg-white object-contain p-1" />
        )}
        <p className="text-xl font-bold">{shop.name}</p>
      </div>
      <div className="card space-y-4">
        <div>
          <p className="text-sm text-slate-500">Ticket #{ticket.number} · {ticket.device_model}</p>
          <h1 className="text-2xl font-bold">{STATUS_LABEL[status]}</h1>
          <p className="text-lg text-slate-700">{CUSTOMER_STATUS_TEXT[status]}</p>
        </div>
        <ol className="space-y-2">
          {STATUSES.map((s, i) => (
            <li key={s} className={`flex items-center gap-3 ${i > current ? "text-slate-400" : "font-semibold"}`}>
              <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-sm ${i < current ? "bg-green-600 text-white" : i === current ? "bg-blue-700 text-white" : "bg-slate-200"}`}>
                {i < current ? "✓" : i + 1}
              </span>
              {STATUS_LABEL[s]}
            </li>
          ))}
        </ol>
        {price && (
          <p className="rounded-xl bg-slate-100 p-3 text-lg">
            {price.kind === "final" ? "Total due" : "Quoted total"}: <strong>{formatMoney(price.total_cents)}</strong>
          </p>
        )}
        <p className="text-sm text-slate-500">
          Last updated {new Date(ticket.updated_at).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" })} UTC
        </p>
      </div>
      {shop.phone && <a className="btn btn-secondary w-full" href={`tel:${shop.phone}`}>Call {shop.name}</a>}
    </main>
  );
}
