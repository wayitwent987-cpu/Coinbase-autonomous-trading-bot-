import { describe, expect, it } from "vitest";
import { computeTotals, dollarsToCents, formatMoney, linePriceCents, sanitizeLines, type QuoteLine, type Rates } from "../src/lib/quote-math";

const rates: Rates = { labor_rate_cents: 8000, parts_markup_pct: 30, tax_rate_pct: 8.25, tax_labor: false };

describe("quote math", () => {
  it("adds the markup to parts", () => {
    // $40 part + 30% = $52
    expect(linePriceCents({ kind: "part", description: "Screen", qty: 1, unit_cost_cents: 4000 }, rates)).toBe(5200);
    expect(linePriceCents({ kind: "part", description: "Screw", qty: 3, unit_cost_cents: 150 }, rates)).toBe(585);
  });

  it("charges labor by the hour", () => {
    expect(linePriceCents({ kind: "labor", description: "Labor", hours: 1.5 }, rates)).toBe(12000);
    expect(linePriceCents({ kind: "labor", description: "Labor", hours: 0.25 }, rates)).toBe(2000);
  });

  it("totals a typical quote, taxing parts only", () => {
    const lines: QuoteLine[] = [
      { kind: "part", description: "Screen", qty: 1, unit_cost_cents: 4000 },
      { kind: "labor", description: "Fit screen", hours: 1 },
      { kind: "fee", description: "Diagnostic fee", amount_cents: 2500 },
    ];
    const t = computeTotals(lines, rates);
    expect(t.parts_cents).toBe(5200);
    expect(t.labor_cents).toBe(8000);
    expect(t.fees_cents).toBe(2500);
    expect(t.subtotal_cents).toBe(15700);
    expect(t.tax_cents).toBe(429); // 8.25% of $52.00
    expect(t.total_cents).toBe(16129);
  });

  it("taxes everything when the shop turns that on", () => {
    const t = computeTotals(
      [
        { kind: "part", description: "Screen", qty: 1, unit_cost_cents: 4000 },
        { kind: "labor", description: "Fit", hours: 1 },
      ],
      { ...rates, tax_labor: true, tax_rate_pct: 10 },
    );
    expect(t.tax_cents).toBe(1320);
    expect(t.total_cents).toBe(14520);
  });

  it("handles discounts and never taxes a negative amount", () => {
    const t = computeTotals([{ kind: "fee", description: "Discount", amount_cents: -1000 }], { ...rates, tax_labor: true });
    expect(t.subtotal_cents).toBe(-1000);
    expect(t.tax_cents).toBe(0);
  });

  it("is zero for an empty quote", () => {
    expect(computeTotals([], rates).total_cents).toBe(0);
  });

  it("reads money typed by people", () => {
    expect(dollarsToCents("12.50")).toBe(1250);
    expect(dollarsToCents("$1,200")).toBe(120000);
    expect(dollarsToCents("19.99")).toBe(1999);
    expect(dollarsToCents("-10")).toBe(-1000);
    expect(dollarsToCents("abc")).toBe(0);
    expect(dollarsToCents("")).toBe(0);
  });

  it("formats money", () => {
    expect(formatMoney(16129)).toBe("$161.29");
    expect(formatMoney(-1000)).toBe("-$10.00");
    expect(formatMoney(5)).toBe("$0.05");
  });

  it("cleans up bad lines instead of saving them", () => {
    const cleaned = sanitizeLines([
      { kind: "part", description: " Screen ", qty: "2", unit_cost_cents: "4000.4", ai_estimate: true },
      { kind: "part", description: "Negative", qty: -5, unit_cost_cents: -100 },
      { kind: "labor", description: "Labor", hours: "abc" },
      { kind: "labor", description: "", hours: 1 },
      { kind: "hack", description: "x" },
      null,
      "nope",
    ]);
    expect(cleaned).toEqual([
      { kind: "part", description: "Screen", qty: 2, unit_cost_cents: 4000, ai_estimate: true },
      { kind: "part", description: "Negative", qty: 1, unit_cost_cents: 0 },
      { kind: "labor", description: "Labor", hours: 0 },
    ]);
    expect(sanitizeLines("not a list")).toEqual([]);
  });
});
