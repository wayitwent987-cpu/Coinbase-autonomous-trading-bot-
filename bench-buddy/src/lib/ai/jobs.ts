// Bookkeeping for AI work so pages can show "working...", errors, and timings.
import "server-only";
import { createAdminClient } from "../supabase/admin";
import type { AiJobKind } from "../types";
import { toAiError } from "./client";

type TicketRef = { id: string; shop_id: string };

/** Mark a job as running. Call this BEFORE responding so the page shows a spinner. */
export async function beginJob(ticket: TicketRef, kind: AiJobKind) {
  const admin = createAdminClient();
  const { error } = await admin.from("ai_jobs").upsert({
    ticket_id: ticket.id,
    shop_id: ticket.shop_id,
    kind,
    status: "running",
    error: null,
    started_at: new Date().toISOString(),
    finished_at: null,
    duration_ms: null,
  });
  if (error) throw new Error(`Could not start ${kind}: ${error.message}`);
}

/** Do the work, then record success or a friendly error. Never throws. */
export async function finishJob(ticket: TicketRef, kind: AiJobKind, work: () => Promise<void>) {
  const admin = createAdminClient();
  const started = Date.now();
  let failure: string | null = null;
  try {
    await work();
  } catch (error) {
    failure = toAiError(error).message;
  }
  await admin
    .from("ai_jobs")
    .update({
      status: failure ? "error" : "done",
      error: failure,
      finished_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
    })
    .match({ ticket_id: ticket.id, kind });
}

