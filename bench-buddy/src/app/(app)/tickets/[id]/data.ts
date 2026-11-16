import "server-only";
import { notFound } from "next/navigation";
import { cache } from "react";
import { requireShop } from "@/lib/shop";
import { createClient } from "@/lib/supabase/server";
import type { AiJob, AiJobKind, QuoteRow, Ticket } from "@/lib/types";

/** Loads one of the logged-in shop's tickets, or shows "not found". */
export const loadTicket = cache(async (id: string) => {
  const { shop } = await requireShop();
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();
  const supabase = await createClient();
  const [{ data: ticket }, { data: jobs }, { data: quotes }] = await Promise.all([
    supabase.from("tickets").select("*").eq("id", id).maybeSingle(),
    supabase.from("ai_jobs").select("*").eq("ticket_id", id),
    supabase.from("quotes").select("*").eq("ticket_id", id),
  ]);
  if (!ticket) notFound();
  const job = (kind: AiJobKind) => (jobs as AiJob[] | null)?.find((j) => j.kind === kind);
  const quoteRows = (quotes ?? []) as QuoteRow[];
  return {
    shop,
    supabase,
    ticket: ticket as Ticket,
    job,
    quote: quoteRows.find((q) => q.kind === "quote") ?? null,
    final: quoteRows.find((q) => q.kind === "final") ?? null,
  };
});
