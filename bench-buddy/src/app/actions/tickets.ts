"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { after } from "next/server";
import { runGuide } from "@/lib/ai/guide";
import { beginJob, finishJob } from "@/lib/ai/jobs";
import { runQuoteDraft } from "@/lib/ai/quote";
import { runTriage } from "@/lib/ai/triage";
import { buildEmailContext, sendCustomerEmail } from "@/lib/email/send";
import { statusEmail } from "@/lib/email/templates";
import { requireShop } from "@/lib/shop";
import { createAdminClient } from "@/lib/supabase/admin";
import { createClient } from "@/lib/supabase/server";
import { isStatus } from "@/lib/types";

/**
 * Loads a ticket through the logged-in shop's own connection. The database
 * security rules mean this returns nothing for another shop's ticket, so
 * every action below starts here.
 */
async function getOwnTicket(ticketId: string) {
  const { shop } = await requireShop();
  const supabase = await createClient();
  const { data: ticket } = await supabase
    .from("tickets")
    .select("id, shop_id, number, public_token, customer_name, customer_email, device_model, status")
    .eq("id", ticketId)
    .maybeSingle();
  if (!ticket) throw new Error("Ticket not found");
  return { shop, ticket, supabase };
}

/** Move a ticket to another column and email the customer about it. */
export async function moveTicket(ticketId: string, formData: FormData) {
  const status = formData.get("status");
  if (!isStatus(status)) return;
  const { shop, ticket, supabase } = await getOwnTicket(ticketId);
  if (ticket.status === status) return;

  const { error } = await supabase.from("tickets").update({ status }).eq("id", ticket.id);
  if (error) throw new Error(error.message);

  // Send the email after the page has already updated, so the board stays fast.
  after(async () => {
    const email = statusEmail(status, await buildEmailContext(shop, ticket));
    if (email) await sendCustomerEmail(shop, ticket, `status:${status}`, email);
  });
  revalidatePath("/board");
  revalidatePath(`/tickets/${ticket.id}`);
}

export async function saveNotes(ticketId: string, formData: FormData) {
  const { ticket, supabase } = await getOwnTicket(ticketId);
  const tech_notes = String(formData.get("tech_notes") ?? "").slice(0, 5000);
  const { error } = await supabase.from("tickets").update({ tech_notes }).eq("id", ticket.id);
  if (error) throw new Error(error.message);
  revalidatePath(`/tickets/${ticket.id}`);
}

export async function startTriage(ticketId: string) {
  const { ticket } = await getOwnTicket(ticketId);
  await beginJob(ticket, "triage");
  after(() => finishJob(ticket, "triage", () => runTriage(ticket.id)));
  revalidatePath(`/tickets/${ticket.id}`);
}

function readRepair(formData: FormData): string {
  return String(formData.get("repair") ?? "").trim().slice(0, 300);
}

export async function startGuide(ticketId: string, formData: FormData) {
  const repair = readRepair(formData);
  if (!repair) return;
  const { ticket } = await getOwnTicket(ticketId);
  await beginJob(ticket, "guide");
  after(() => finishJob(ticket, "guide", () => runGuide(ticket.id, repair)));
  redirect(`/tickets/${ticket.id}/guide`);
}

export async function startQuoteDraft(ticketId: string, formData: FormData) {
  const repair = readRepair(formData);
  if (!repair) return;
  const { ticket } = await getOwnTicket(ticketId);
  await beginJob(ticket, "quote");
  after(() => finishJob(ticket, "quote", () => runQuoteDraft(ticket.id, repair)));
  redirect(`/tickets/${ticket.id}/quote`);
}

export async function deleteTicket(ticketId: string) {
  const { ticket, supabase } = await getOwnTicket(ticketId);
  // Remove the customer's photos from storage too, not just the database row.
  const admin = createAdminClient();
  const { data: photos } = await supabase.from("ticket_photos").select("path").eq("ticket_id", ticket.id);
  if (photos?.length) await admin.storage.from("ticket-photos").remove(photos.map((p) => p.path));
  const { error } = await supabase.from("tickets").delete().eq("id", ticket.id);
  if (error) throw new Error(error.message);
  revalidatePath("/board");
  redirect("/board");
}
