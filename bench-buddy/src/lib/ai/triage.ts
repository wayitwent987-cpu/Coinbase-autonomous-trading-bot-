import "server-only";
import { createAdminClient } from "../supabase/admin";
import { askStructured, type UserContent } from "./client";
import { TRIAGE_MAX_PHOTOS, TRIAGE_PROMPT } from "./prompts";
import { TriageSchema } from "./schemas";

export function describeTicket(t: {
  device_type: string;
  device_model: string;
  problem: string;
  tech_notes?: string | null;
}): string {
  return [
    `Device type: ${t.device_type}`,
    `Model (as the customer wrote it): ${t.device_model}`,
    `Problem, in the customer's words: ${t.problem}`,
    t.tech_notes?.trim() ? `Technician's own notes so far: ${t.tech_notes.trim()}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

/** Runs triage for one ticket and saves the result on the ticket. */
export async function runTriage(ticketId: string) {
  const admin = createAdminClient();
  const { data: ticket, error } = await admin
    .from("tickets")
    .select("id, device_type, device_model, problem, tech_notes")
    .eq("id", ticketId)
    .single();
  if (error || !ticket) throw new Error("Ticket not found");

  const content: UserContent = [];

  // Attach a few customer photos so the AI can spot bulges, cracks and corrosion.
  const { data: photos } = await admin
    .from("ticket_photos")
    .select("path")
    .eq("ticket_id", ticketId)
    .order("created_at")
    .limit(TRIAGE_MAX_PHOTOS);
  for (const photo of photos ?? []) {
    const { data: file } = await admin.storage.from("ticket-photos").download(photo.path);
    if (!file) continue;
    const media_type = file.type === "image/png" ? "image/png" : file.type === "image/webp" ? "image/webp" : "image/jpeg";
    content.push({
      type: "image",
      source: { type: "base64", media_type, data: Buffer.from(await file.arrayBuffer()).toString("base64") },
    });
  }

  content.push({ type: "text", text: describeTicket(ticket) });

  const triage = await askStructured("triage", TRIAGE_PROMPT, content, TriageSchema);
  // Keep exactly what the tech should see: at most 3 causes, most likely first.
  triage.causes = triage.causes
    .sort((a, b) => b.probability_pct - a.probability_pct)
    .slice(0, 3)
    .map((c) => ({ ...c, probability_pct: Math.max(0, Math.min(100, Math.round(c.probability_pct))) }));

  const { error: saveError } = await admin.from("tickets").update({ triage }).eq("id", ticketId);
  if (saveError) throw new Error(saveError.message);
}
