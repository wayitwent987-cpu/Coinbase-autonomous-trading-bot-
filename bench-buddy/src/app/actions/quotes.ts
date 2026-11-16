"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { buildEmailContext, sendCustomerEmail } from "@/lib/email/send";
import { quoteEmail } from "@/lib/email/templates";
import { computeTotals, linePriceCents, sanitizeLines, type QuoteLine, type Rates } from "@/lib/quote-math";
import { requireShop } from "@/lib/shop";
import { createClient } from "@/lib/supabase/server";
import type { QuoteKind, Shop } from "@/lib/types";

function shopRates(shop: Shop): Rates {
  return {
    labor_rate_cents: shop.labor_rate_cents,
    parts_markup_pct: Number(shop.parts_markup_pct),
    tax_rate_pct: Number(shop.tax_rate_pct),
    tax_labor: shop.tax_labor,
  };
}

function isKind(value: unknown): value is QuoteKind {
  return value === "quote" || value === "final";
}

/** Start an empty quote, or start the final bill as a copy of the quote. */
export async function createQuote(ticketId: string, kind: QuoteKind) {
  if (!isKind(kind)) return;
  const { shop } = await requireShop();
  const supabase = await createClient();
  const { data: ticket } = await supabase.from("tickets").select("id").eq("id", ticketId).maybeSingle();
  if (!ticket) throw new Error("Ticket not found");

  const rates = shopRates(shop);
  let lines: QuoteLine[] =
    shop.diagnostic_fee_cents > 0
      ? [{ kind: "fee", description: "Diagnostic fee", amount_cents: shop.diagnostic_fee_cents }]
      : [];
  let repair_title = "";
  let customer_note = "";
  if (kind === "final") {
    const { data: quote } = await supabase
      .from("quotes")
      .select("lines, repair_title")
      .eq("ticket_id", ticketId)
      .eq("kind", "quote")
      .maybeSingle();
    if (quote) {
      lines = sanitizeLines(quote.lines);
      repair_title = quote.repair_title;
    }
    customer_note = "Thanks for your business!";
  }
  const totals = computeTotals(lines, rates);
  // "ignoreDuplicates": if one already exists, leave it alone.
  await supabase.from("quotes").upsert(
    {
      ticket_id: ticketId,
      shop_id: shop.id,
      kind,
      status: "draft",
      repair_title,
      customer_note,
      lines,
      ...rates,
      subtotal_cents: totals.subtotal_cents,
      tax_cents: totals.tax_cents,
      total_cents: totals.total_cents,
    },
    { onConflict: "ticket_id,kind", ignoreDuplicates: true },
  );
  redirect(`/tickets/${ticketId}/quote${kind === "final" ? "?kind=final" : ""}`);
}

export type SaveQuoteInput = {
  ticketId: string;
  kind: QuoteKind;
  repair_title: string;
  customer_note: string;
  lines: unknown;
  /** "save" keeps it a draft. "approve" locks in the price and emails the customer. */
  intent: "save" | "approve";
};
export type SaveQuoteResult = { ok: true; message: string } | { ok: false; error: string };

export async function saveQuote(input: SaveQuoteInput): Promise<SaveQuoteResult> {
  if (!isKind(input.kind)) return { ok: false, error: "Unknown quote type." };
  const { shop } = await requireShop();
  const supabase = await createClient();
  const { data: ticket } = await supabase
    .from("tickets")
    .select("id, number, public_token, customer_name, customer_email, device_model")
    .eq("id", input.ticketId)
    .maybeSingle();
  if (!ticket) return { ok: false, error: "Ticket not found." };

  // Never trust numbers from the browser: clean the lines and redo the math
  // here on the server with the shop's current rates.
  const lines = sanitizeLines(input.lines);
  const rates = shopRates(shop);
  const totals = computeTotals(lines, rates);
  const approve = input.intent === "approve";
  if (approve && lines.length === 0) return { ok: false, error: "Add at least one line before approving." };
  if (approve && totals.total_cents < 0) return { ok: false, error: "The total can't be negative." };

  const repair_title = String(input.repair_title ?? "").trim().slice(0, 120);
  const customer_note = String(input.customer_note ?? "").trim().slice(0, 1000);
  // Once approved, the tech has confirmed the prices, so they're no longer "AI guesses".
  const savedLines = approve ? lines.map((l) => (l.kind === "part" ? { ...l, ai_estimate: undefined } : l)) : lines;

  const { error } = await supabase
    .from("quotes")
    .update({
      repair_title,
      customer_note,
      lines: savedLines,
      ...rates,
      subtotal_cents: totals.subtotal_cents,
      tax_cents: totals.tax_cents,
      total_cents: totals.total_cents,
      status: approve ? "approved" : "draft",
    })
    .match({ ticket_id: ticket.id, kind: input.kind });
  if (error) return { ok: false, error: error.message };

  revalidatePath(`/tickets/${ticket.id}`, "layout");
  if (!approve) return { ok: true, message: "Draft saved." };

  const isFinal = input.kind === "final";
  const email = quoteEmail(await buildEmailContext(shop, ticket), {
    isFinal,
    repairTitle: repair_title,
    lines: lines.map((l) => ({
      description: l.kind === "part" && l.qty > 1 ? `${l.description} x${l.qty}` : l.description,
      priceCents: linePriceCents(l, rates),
    })),
    subtotalCents: totals.subtotal_cents,
    taxCents: totals.tax_cents,
    totalCents: totals.total_cents,
    note: customer_note,
  });
  const result = await sendCustomerEmail(shop, ticket, isFinal ? "final" : "quote", email);
  if (result === "sent") {
    await supabase.from("quotes").update({ status: "sent" }).match({ ticket_id: ticket.id, kind: input.kind });
    return { ok: true, message: `Approved and emailed to ${ticket.customer_email}.` };
  }
  return {
    ok: true,
    message:
      result === "skipped"
        ? "Approved. No email was sent (no customer email on file, or email isn't set up yet), so tell the customer yourself."
        : "Approved, but the email failed to send. Check the email log on the ticket and tell the customer yourself.",
  };
}

/** Unlock an approved quote so it can be changed. */
export async function reopenQuote(ticketId: string, kind: QuoteKind) {
  if (!isKind(kind)) return;
  await requireShop();
  const supabase = await createClient();
  await supabase.from("quotes").update({ status: "draft" }).match({ ticket_id: ticketId, kind });
  revalidatePath(`/tickets/${ticketId}`, "layout");
}
