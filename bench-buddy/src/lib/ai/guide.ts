import "server-only";
import type Anthropic from "@anthropic-ai/sdk";
import { createAdminClient } from "../supabase/admin";
import { anthropic, askStructured } from "./client";
import { AI_SETTINGS, GUIDE_PROMPT, GUIDE_RESEARCH_PROMPT, GUIDE_USES_WEB_SEARCH } from "./prompts";
import { GuideSchema, type StoredGuide } from "./schemas";
import { describeTicket } from "./triage";

type Research = { notes: string; sources: { title: string; url: string }[] };

/**
 * Step A: let the AI search the web for model-specific repair info.
 * Returns null if search is off or fails; the guide is then written from the
 * AI's own knowledge and marked "not web-checked".
 */
async function research(task: string): Promise<Research | null> {
  if (!GUIDE_USES_WEB_SEARCH) return null;
  try {
    const messages: Anthropic.MessageParam[] = [{ role: "user", content: task }];
    const sources = new Map<string, string>();
    let notes = "";
    // A long search can "pause"; we send it back to continue, a few times at most.
    for (let turn = 0; turn < 4; turn++) {
      const response = await anthropic().messages.create({
        model: AI_SETTINGS.guide.model,
        max_tokens: 8000,
        system: GUIDE_RESEARCH_PROMPT,
        tools: [{ type: "web_search_20260209", name: "web_search", max_uses: 5 }],
        output_config: { effort: "low" },
        messages,
      });
      for (const block of response.content) {
        if (block.type === "text") notes += block.text;
        if (block.type === "web_search_tool_result" && Array.isArray(block.content)) {
          for (const result of block.content) {
            if (result.type === "web_search_result" && /^https?:\/\//.test(result.url)) {
              sources.set(result.url, result.title || result.url);
            }
          }
        }
      }
      if (response.stop_reason !== "pause_turn") break;
      messages.push({ role: "assistant", content: response.content });
    }
    if (!notes.trim()) return null;
    return {
      notes: notes.trim(),
      sources: [...sources].slice(0, 8).map(([url, title]) => ({ url, title })),
    };
  } catch (error) {
    console.error("[guide] web research failed, continuing without it:", error);
    return null;
  }
}

/** Builds a step-by-step repair guide for one ticket and saves it on the ticket. */
export async function runGuide(ticketId: string, repair: string) {
  const admin = createAdminClient();
  const { data: ticket, error } = await admin
    .from("tickets")
    .select("id, device_type, device_model, problem, tech_notes, triage")
    .eq("id", ticketId)
    .single();
  if (error || !ticket) throw new Error("Ticket not found");

  const task = [`Repair to perform: ${repair}`, describeTicket(ticket)].join("\n");
  const found = await research(task);

  const prompt = [
    task,
    ticket.triage ? `Earlier triage summary: ${ticket.triage.summary}` : null,
    found
      ? `Research notes from a web search (prefer these over memory):\n<research_notes>\n${found.notes}\n</research_notes>`
      : "No web research is available for this guide, so rely on what you know and be especially honest in verify_first.",
  ]
    .filter(Boolean)
    .join("\n\n");

  const guide = await askStructured("guide", GUIDE_PROMPT, [{ type: "text", text: prompt }], GuideSchema);

  const stored: StoredGuide = {
    ...guide,
    est_minutes: Math.max(0, Math.round(guide.est_minutes)),
    for_repair: repair,
    web_checked: Boolean(found),
    sources: found?.sources ?? [],
    generated_at: new Date().toISOString(),
  };
  const { error: saveError } = await admin.from("tickets").update({ guide: stored }).eq("id", ticketId);
  if (saveError) throw new Error(saveError.message);
}
