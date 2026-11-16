// The exact shape of the answers we ask the AI for.
import { z } from "zod";

const SafetyFlagSchema = z.object({
  type: z.enum(["swollen_battery", "liquid_damage", "data_loss_risk", "other"]),
  severity: z.enum(["stop", "caution"]),
  note: z.string(),
});

const Confidence = z.enum(["high", "medium", "low"]);

export const TriageSchema = z.object({
  summary: z.string(),
  safety_flags: z.array(SafetyFlagSchema),
  causes: z.array(
    z.object({
      title: z.string(),
      probability_pct: z.number(),
      why: z.string(),
      fix: z.string(),
    }),
  ),
  questions_to_ask: z.array(z.string()),
  tests_to_run: z.array(z.string()),
  likely_parts: z.array(z.string()),
  difficulty: z.enum(["easy", "moderate", "hard", "specialist"]),
  difficulty_note: z.string(),
  confidence: Confidence,
  uncertain_about: z.string(),
});
export type Triage = z.infer<typeof TriageSchema>;

export const GuideSchema = z.object({
  title: z.string(),
  summary: z.string(),
  confidence: Confidence,
  safety_warnings: z.array(z.string()),
  before_you_start: z.array(z.string()),
  tools: z.array(z.string()),
  parts: z.array(z.string()),
  est_minutes: z.number(),
  steps: z.array(
    z.object({
      title: z.string(),
      detail: z.string(),
      caution: z.string(),
    }),
  ),
  test_checklist: z.array(z.string()),
  verify_first: z.array(z.string()),
});
export type Guide = z.infer<typeof GuideSchema>;

/** A guide as saved on the ticket, with where it came from. */
export type StoredGuide = Guide & {
  for_repair: string;
  web_checked: boolean;
  sources: { title: string; url: string }[];
  generated_at: string;
};

export const QuoteDraftSchema = z.object({
  repair_title: z.string(),
  parts: z.array(
    z.object({
      description: z.string(),
      qty: z.number(),
      est_unit_cost_usd: z.number(),
    }),
  ),
  labor: z.array(
    z.object({
      description: z.string(),
      hours: z.number(),
    }),
  ),
  customer_note: z.string(),
  assumptions: z.array(z.string()),
  price_confidence: Confidence,
});
export type QuoteDraft = z.infer<typeof QuoteDraftSchema>;
