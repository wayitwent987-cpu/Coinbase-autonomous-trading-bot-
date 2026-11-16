// A simple keyword safety net that runs the moment a ticket is created.
// It does NOT use AI, so these warnings show even if the AI is down or wrong.
import type { SafetyFlag } from "./types";

const RULES: { type: SafetyFlag["type"]; severity: SafetyFlag["severity"]; pattern: RegExp; note: string }[] = [
  {
    type: "swollen_battery",
    severity: "stop",
    pattern:
      /\b(swollen|swelling|swelled|bulg(e|ed|ing)|puff(y|ed|ing)|bloat(ed|ing)|expand(ed|ing)|screen (is )?(lifting|popping|coming (off|up))|back (is )?(lifting|popping|coming off)|smok(e|ing)|burning smell|caught fire|on fire|very hot|overheat(s|ed|ing)?|hissing)\b/i,
    note: "Possible swollen or damaged battery. Do not charge, press on, or puncture. Store in a fire-safe place.",
  },
  {
    type: "liquid_damage",
    severity: "caution",
    pattern:
      /\b(water|wet|liquid|spill(ed|t)?|coffee|soda|juice|beer|wine|tea|rain|pool|toilet|sink|bath|shower|ocean|sea|lake|washing machine|dropped (it )?in|submerged|moisture|humid|corrosion|corroded)\b/i,
    note: "Possible liquid damage. Do not power on or charge until opened and inspected for corrosion.",
  },
  {
    type: "data_loss_risk",
    severity: "caution",
    pattern:
      /\b(photos?|pictures?|data|files?|backup|backed up|back up|recover(y)?|contacts|documents?|won'?t (turn on|boot|start)|not (turning on|booting)|dead|boot ?loop|clicking|hard drive|ssd|forgot (my )?(password|passcode)|locked out|factory reset)\b/i,
    note: "Data may be at risk. Ask about backups and get the customer's OK before any step that could erase data.",
  },
];

export function keywordSafetyFlags(text: string): SafetyFlag[] {
  return RULES.filter((r) => r.pattern.test(text)).map(({ type, severity, note }) => ({ type, severity, note }));
}

export const FLAG_LABEL: Record<SafetyFlag["type"], string> = {
  swollen_battery: "Battery hazard",
  liquid_damage: "Liquid damage",
  data_loss_risk: "Data-loss risk",
  other: "Safety",
};

/** Keyword flags + AI flags, without repeating the same type twice (AI note wins). */
export function mergeFlags(keyword: SafetyFlag[], ai: SafetyFlag[] | undefined): SafetyFlag[] {
  const byType = new Map<string, SafetyFlag>();
  for (const f of keyword) byType.set(f.type, f);
  for (const f of ai ?? []) {
    const prev = byType.get(f.type);
    // Never let the AI downgrade a "stop" the keyword check raised.
    const severity = prev?.severity === "stop" ? "stop" : f.severity;
    byType.set(f.type === "other" ? `other:${f.note}` : f.type, { ...f, severity });
  }
  return [...byType.values()].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "stop" ? -1 : 1));
}
