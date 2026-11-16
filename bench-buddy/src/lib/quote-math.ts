// All the money math lives here, in plain code. The AI never does arithmetic.
// Money is kept in whole cents so we never get 0.1 + 0.2 = 0.30000000000000004.

export type QuoteLine =
  // A part: you enter what it costs YOU; the shop markup is added on top.
  | { kind: "part"; description: string; qty: number; unit_cost_cents: number; ai_estimate?: boolean }
  // Labor: hours times the shop's hourly rate.
  | { kind: "labor"; description: string; hours: number }
  // A flat amount: diagnostic fee, shipping, or a discount (negative number).
  | { kind: "fee"; description: string; amount_cents: number };

export type Rates = {
  labor_rate_cents: number;
  parts_markup_pct: number;
  tax_rate_pct: number;
  tax_labor: boolean;
};

export type Totals = {
  parts_cents: number;
  labor_cents: number;
  fees_cents: number;
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
};

/** What the customer pays for one line, before tax. */
export function linePriceCents(line: QuoteLine, rates: Rates): number {
  switch (line.kind) {
    case "part":
      return Math.round(line.qty * line.unit_cost_cents * (1 + rates.parts_markup_pct / 100));
    case "labor":
      return Math.round(line.hours * rates.labor_rate_cents);
    case "fee":
      return Math.round(line.amount_cents);
  }
}

export function computeTotals(lines: QuoteLine[], rates: Rates): Totals {
  let parts = 0;
  let labor = 0;
  let fees = 0;
  for (const line of lines) {
    const price = linePriceCents(line, rates);
    if (line.kind === "part") parts += price;
    else if (line.kind === "labor") labor += price;
    else fees += price;
  }
  const subtotal = parts + labor + fees;
  // Tax: always on parts; on labor and fees only if the shop turned that on.
  const taxable = Math.max(0, rates.tax_labor ? subtotal : parts);
  const tax = Math.round((taxable * rates.tax_rate_pct) / 100);
  return {
    parts_cents: parts,
    labor_cents: labor,
    fees_cents: fees,
    subtotal_cents: subtotal,
    tax_cents: tax,
    total_cents: subtotal + tax,
  };
}

export function formatMoney(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  return `${sign}$${(Math.abs(cents) / 100).toFixed(2)}`;
}

/** "12.50" or "$1,200" typed by a person -> 1250 / 120000 cents. Bad input -> 0. */
export function dollarsToCents(input: unknown): number {
  const n = Number(String(input ?? "").replace(/[$,\s]/g, ""));
  return Number.isFinite(n) ? Math.round(n * 100) : 0;
}

export function centsToDollars(cents: number): string {
  return (cents / 100).toFixed(2);
}

/** Cleans up lines coming from a form or the AI so bad data can never be saved. */
export function sanitizeLines(input: unknown): QuoteLine[] {
  if (!Array.isArray(input)) return [];
  const out: QuoteLine[] = [];
  for (const raw of input.slice(0, 40)) {
    if (!raw || typeof raw !== "object") continue;
    const r = raw as Record<string, unknown>;
    const description = String(r.description ?? "").trim().slice(0, 200);
    if (!description) continue;
    const num = (v: unknown, min: number, max: number) => {
      const n = Number(v);
      return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : 0;
    };
    if (r.kind === "part") {
      out.push({
        kind: "part",
        description,
        qty: Math.max(1, Math.round(num(r.qty, 1, 999))),
        unit_cost_cents: Math.round(num(r.unit_cost_cents, 0, 100_000_00)),
        ...(r.ai_estimate ? { ai_estimate: true } : {}),
      });
    } else if (r.kind === "labor") {
      out.push({ kind: "labor", description, hours: Math.round(num(r.hours, 0, 200) * 100) / 100 });
    } else if (r.kind === "fee") {
      out.push({
        kind: "fee",
        description,
        amount_cents: Math.round(num(r.amount_cents, -100_000_00, 100_000_00)),
      });
    }
  }
  return out;
}
