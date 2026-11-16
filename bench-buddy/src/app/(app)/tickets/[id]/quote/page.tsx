import type { Metadata } from "next";
import Link from "next/link";
import { createQuote } from "@/app/actions/quotes";
import { AutoRefresh } from "@/components/auto-refresh";
import { QuoteEditor } from "@/components/quote-editor";
import { SubmitButton } from "@/components/submit-button";
import { ErrorNote, Working } from "@/components/working";
import { jobView } from "@/lib/ai/job-state";
import { sanitizeLines, type Rates } from "@/lib/quote-math";
import { loadTicket } from "../data";

export const metadata: Metadata = { title: "Quote" };

export default async function QuotePage({ params, searchParams }: PageProps<"/tickets/[id]/quote">) {
  const { id } = await params;
  const kind = (await searchParams).kind === "final" ? "final" : "quote";
  const { shop, ticket, job, quote, final } = await loadTicket(id);
  const row = kind === "final" ? final : quote;
  const state = kind === "quote" ? jobView(job("quote")) : { state: "none" as const };
  const locked = row ? row.status !== "draft" : false;

  // Drafts always use the shop's current prices. Approved ones keep the prices
  // they were approved with, even if Settings change later.
  const source = row && locked ? row : shop;
  const rates: Rates = {
    labor_rate_cents: source.labor_rate_cents,
    parts_markup_pct: Number(source.parts_markup_pct),
    tax_rate_pct: Number(source.tax_rate_pct),
    tax_labor: source.tax_labor,
  };

  return (
    <div className="mx-auto max-w-2xl space-y-4 pb-10">
      <AutoRefresh active={state.state === "running"} />
      <Link href={`/tickets/${ticket.id}`} className="text-sm font-semibold text-blue-700">← Back to ticket #{ticket.number}</Link>
      <header>
        <p className="text-sm font-semibold text-slate-500">{ticket.device_model} · {ticket.customer_name}</p>
        <h1 className="text-3xl font-extrabold">{kind === "final" ? "Final price" : "Quote"}</h1>
        {row && (
          <p className="text-slate-600">
            {row.status === "draft"
              ? kind === "final"
                ? "Enter what you actually used. Nothing is sent until you approve."
                : "Draft. Check every line. Nothing is sent until you approve."
              : row.status === "sent" ? "Approved and emailed to the customer." : "Approved."}
          </p>
        )}
      </header>

      {state.state === "running" && (
        <div className="card"><Working>Drafting the quote from your rates… about 15 seconds.</Working></div>
      )}
      {state.state === "error" && <ErrorNote>{state.error}</ErrorNote>}

      {!row && state.state !== "running" && (
        <form action={createQuote.bind(null, ticket.id, kind)} className="card space-y-2">
          <p>{kind === "final" ? "No final price yet." : "No quote yet."}</p>
          <SubmitButton className="btn btn-primary" pendingText="Opening…">{kind === "final" ? "Work out the final price" : "Start a quote by hand"}</SubmitButton>
        </form>
      )}

      {row && state.state !== "running" && (
        <QuoteEditor
          // Start the editor fresh only when the AI writes a new draft, so your
          // own edits and messages aren't wiped by a background refresh.
          key={`${row.id}:${kind === "quote" ? (job("quote")?.finished_at ?? "") : ""}`}
          ticketId={ticket.id}
          kind={kind}
          locked={locked}
          rates={rates}
          initial={{ repair_title: row.repair_title, customer_note: row.customer_note, lines: sanitizeLines(row.lines) }}
          hasCustomerEmail={Boolean(ticket.customer_email)}
          quotedTotalCents={kind === "final" && quote ? quote.total_cents : null}
          aiNotes={row.ai_notes}
        />
      )}
    </div>
  );
}
