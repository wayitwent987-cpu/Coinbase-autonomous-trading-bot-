import "server-only";
import { computeTotals, sanitizeLines, type QuoteLine } from "../quote-math";
import { createAdminClient } from "../supabase/admin";
import { askStructured } from "./client";
import { QUOTE_PROMPT } from "./prompts";
import { QuoteDraftSchema } from "./schemas";
import { describeTicket } from "./triage";

/**
 * Asks the AI which parts and how much labor a repair needs, then builds a
 * DRAFT quote using the shop's own rates. Nothing is sent to the customer.
 */
export async function runQuoteDraft(ticketId: string, repair: string) {
  const admin = createAdminClient();
  const { data: ticket, error } = await admin
    .from("tickets")
    .select("id, shop_id, device_type, device_model, problem, tech_notes, triage")
    .eq("id", ticketId)
    .single();
  if (error || !ticket) throw new Error("Ticket not found");
  const { data: shop } = await admin.from("shops").select("*").eq("id", ticket.shop_id).single();
  if (!shop) throw new Error("Shop not found");

  const prompt = [
    `Repair to quote: ${repair}`,
    describeTicket(ticket),
    ticket.triage?.likely_parts?.length ? `Parts suggested at triage: ${ticket.triage.likely_parts.join(", ")}` : null,
  ]
    .filter(Boolean)
    .join("\n");

  const draft = await askStructured("quote", QUOTE_PROMPT, [{ type: "text", text: prompt }], QuoteDraftSchema);

  const lines: QuoteLine[] = sanitizeLines([
    ...draft.parts.map((p) => ({
      kind: "part",
      description: p.description,
      qty: p.qty,
      unit_cost_cents: Math.round(p.est_unit_cost_usd * 100),
      ai_estimate: true,
    })),
    ...draft.labor.map((l) => ({ kind: "labor", description: l.description, hours: l.hours })),
    ...(shop.diagnostic_fee_cents > 0
      ? [{ kind: "fee", description: "Diagnostic fee", amount_cents: shop.diagnostic_fee_cents }]
      : []),
  ]);

  const rates = {
    labor_rate_cents: shop.labor_rate_cents,
    parts_markup_pct: Number(shop.parts_markup_pct),
    tax_rate_pct: Number(shop.tax_rate_pct),
    tax_labor: shop.tax_labor,
  };
  const totals = computeTotals(lines, rates);

  const { error: saveError } = await admin.from("quotes").upsert(
    {
      ticket_id: ticket.id,
      shop_id: ticket.shop_id,
      kind: "quote",
      status: "draft",
      repair_title: draft.repair_title.slice(0, 120),
      lines,
      customer_note: draft.customer_note.slice(0, 1000),
      ai_notes: { assumptions: draft.assumptions.slice(0, 8), price_confidence: draft.price_confidence },
      ...rates,
      subtotal_cents: totals.subtotal_cents,
      tax_cents: totals.tax_cents,
      total_cents: totals.total_cents,
    },
    { onConflict: "ticket_id,kind" },
  );
  if (saveError) throw new Error(saveError.message);
}
