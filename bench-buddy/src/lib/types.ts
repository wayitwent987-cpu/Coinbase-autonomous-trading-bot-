// Shared shapes used across the app.

export const STATUSES = [
  "received",
  "diagnosing",
  "waiting_parts",
  "ready",
  "picked_up",
] as const;
export type TicketStatus = (typeof STATUSES)[number];

export const STATUS_LABEL: Record<TicketStatus, string> = {
  received: "Received",
  diagnosing: "Diagnosing",
  waiting_parts: "Waiting on Parts",
  ready: "Ready",
  picked_up: "Picked Up",
};

export function isStatus(value: unknown): value is TicketStatus {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}

export function nextStatus(status: TicketStatus): TicketStatus | null {
  const i = STATUSES.indexOf(status);
  return i < STATUSES.length - 1 ? STATUSES[i + 1] : null;
}

export const DEVICE_TYPES = [
  "Phone",
  "Tablet",
  "Laptop",
  "Desktop PC",
  "Game console",
  "Handheld console",
  "Controller",
  "Smartwatch",
  "Other",
] as const;

export type SafetyFlagType = "swollen_battery" | "liquid_damage" | "data_loss_risk" | "other";
export type SafetyFlag = {
  type: SafetyFlagType;
  severity: "stop" | "caution";
  note: string;
};

export type Shop = {
  id: string;
  owner_id: string;
  name: string;
  slug: string;
  email: string | null;
  phone: string | null;
  logo_path: string | null;
  labor_rate_cents: number;
  diagnostic_fee_cents: number;
  parts_markup_pct: number;
  tax_rate_pct: number;
  tax_labor: boolean;
};

export type Ticket = {
  id: string;
  shop_id: string;
  number: number;
  public_token: string;
  customer_name: string;
  customer_email: string | null;
  customer_phone: string | null;
  device_type: string;
  device_model: string;
  problem: string;
  status: TicketStatus;
  tech_notes: string;
  safety_flags: SafetyFlag[];
  triage: import("./ai/schemas").Triage | null;
  guide: import("./ai/schemas").StoredGuide | null;
  created_at: string;
  updated_at: string;
};

export type AiJobKind = "triage" | "guide" | "quote";
export type AiJob = {
  ticket_id: string;
  kind: AiJobKind;
  status: "running" | "done" | "error";
  error: string | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
};

export type QuoteKind = "quote" | "final";
export type QuoteRow = {
  id: string;
  ticket_id: string;
  shop_id: string;
  kind: QuoteKind;
  status: "draft" | "approved" | "sent";
  repair_title: string;
  lines: import("./quote-math").QuoteLine[];
  customer_note: string;
  ai_notes: { assumptions: string[]; price_confidence: string } | null;
  labor_rate_cents: number;
  parts_markup_pct: number;
  tax_rate_pct: number;
  tax_labor: boolean;
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
  updated_at: string;
};
