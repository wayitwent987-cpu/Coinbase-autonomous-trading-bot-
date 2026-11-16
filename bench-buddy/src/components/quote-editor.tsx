"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { reopenQuote, saveQuote } from "@/app/actions/quotes";
import { centsToDollars, computeTotals, dollarsToCents, formatMoney, linePriceCents, type QuoteLine, type Rates } from "@/lib/quote-math";
import type { QuoteKind } from "@/lib/types";

// While typing we keep money and hours as text so "12." or "" don't jump around.
type EditLine = { key: number; kind: QuoteLine["kind"]; description: string; qty: string; amount: string; ai: boolean };

function toEdit(line: QuoteLine, key: number): EditLine {
  if (line.kind === "part") return { key, kind: "part", description: line.description, qty: String(line.qty), amount: centsToDollars(line.unit_cost_cents), ai: Boolean(line.ai_estimate) };
  if (line.kind === "labor") return { key, kind: "labor", description: line.description, qty: "1", amount: String(line.hours), ai: false };
  return { key, kind: "fee", description: line.description, qty: "1", amount: centsToDollars(line.amount_cents), ai: false };
}

function fromEdit(line: EditLine): QuoteLine {
  if (line.kind === "part") {
    return { kind: "part", description: line.description, qty: Math.max(1, Math.round(Number(line.qty) || 1)), unit_cost_cents: Math.max(0, dollarsToCents(line.amount)), ...(line.ai ? { ai_estimate: true } : {}) };
  }
  if (line.kind === "labor") return { kind: "labor", description: line.description, hours: Math.max(0, Number(line.amount) || 0) };
  return { kind: "fee", description: line.description, amount_cents: dollarsToCents(line.amount) };
}

const AMOUNT_LABEL = { part: "Your cost, each ($)", labor: "Hours", fee: "Amount ($)" } as const;
const KIND_LABEL = { part: "Part", labor: "Labor", fee: "Fee / discount" } as const;

export function QuoteEditor({
  ticketId,
  kind,
  locked,
  rates,
  initial,
  hasCustomerEmail,
  quotedTotalCents,
  aiNotes,
}: {
  ticketId: string;
  kind: QuoteKind;
  /** Approved quotes are read-only until the tech taps "Edit". */
  locked: boolean;
  rates: Rates;
  initial: { repair_title: string; customer_note: string; lines: QuoteLine[] };
  hasCustomerEmail: boolean;
  /** On the final bill: what the customer was quoted, for comparison. */
  quotedTotalCents: number | null;
  aiNotes: { assumptions: string[]; price_confidence: string } | null;
}) {
  const router = useRouter();
  const [title, setTitle] = useState(initial.repair_title);
  const [note, setNote] = useState(initial.customer_note);
  const [lines, setLines] = useState<EditLine[]>(() => initial.lines.map(toEdit));
  const [nextKey, setNextKey] = useState(initial.lines.length);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  const isFinal = kind === "final";
  const real = lines.map(fromEdit);
  const totals = computeTotals(real, rates);
  const aiGuesses = lines.filter((l) => l.ai).length;

  function update(key: number, patch: Partial<EditLine>) {
    setLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  }
  function add(lineKind: QuoteLine["kind"]) {
    setLines((prev) => [...prev, { key: nextKey, kind: lineKind, description: lineKind === "labor" ? "Labor" : "", qty: "1", amount: "", ai: false }]);
    setNextKey((k) => k + 1);
  }
  function submit(intent: "save" | "approve") {
    if (intent === "approve") {
      if (aiGuesses > 0 && !window.confirm(`${aiGuesses} part price${aiGuesses === 1 ? " is" : "s are"} still an AI guess you haven't checked. Approve anyway?`)) return;
      const question = hasCustomerEmail
        ? `Approve ${formatMoney(totals.total_cents)} and email it to the customer?`
        : `Approve ${formatMoney(totals.total_cents)}? (No customer email on file, so nothing is sent.)`;
      if (!window.confirm(question)) return;
    }
    setResult(null);
    startTransition(async () => {
      const response = await saveQuote({ ticketId, kind, repair_title: title, customer_note: note, lines: real, intent });
      setResult(response.ok ? { ok: true, text: response.message } : { ok: false, text: response.error });
      if (response.ok) router.refresh();
    });
  }

  return (
    <div className="space-y-4">
      <fieldset disabled={locked || pending} className="space-y-4">
        <div className="card space-y-3">
          <div>
            <label className="label" htmlFor="repair_title">Job name (the customer sees this)</label>
            <input className="input" id="repair_title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} placeholder="e.g. Screen replacement" />
          </div>
        </div>

        <ul className="space-y-3">
          {lines.map((line, i) => (
            <li key={line.key} className="card space-y-3">
              <div className="flex items-center justify-between gap-2">
                <span className="rounded-md bg-slate-200 px-2 py-0.5 text-xs font-bold uppercase">{KIND_LABEL[line.kind]}</span>
                <button type="button" className="btn btn-danger btn-sm" onClick={() => setLines((prev) => prev.filter((l) => l.key !== line.key))} aria-label={`Remove line ${i + 1}`}>
                  Remove
                </button>
              </div>
              <div>
                <label className="label" htmlFor={`d${line.key}`}>Description</label>
                <input className="input" id={`d${line.key}`} value={line.description} onChange={(e) => update(line.key, { description: e.target.value })} maxLength={200} placeholder={line.kind === "fee" ? "e.g. Diagnostic fee, or Discount" : ""} />
              </div>
              <div className="flex items-end gap-3">
                {line.kind === "part" && (
                  <div className="w-20">
                    <label className="label" htmlFor={`q${line.key}`}>Qty</label>
                    <input className="input" id={`q${line.key}`} inputMode="numeric" value={line.qty} onChange={(e) => update(line.key, { qty: e.target.value })} />
                  </div>
                )}
                <div className="flex-1">
                  <label className="label" htmlFor={`a${line.key}`}>{AMOUNT_LABEL[line.kind]}</label>
                  <input className="input" id={`a${line.key}`} inputMode="decimal" value={line.amount} onChange={(e) => update(line.key, { amount: e.target.value, ai: false })} placeholder={line.kind === "fee" ? "Use a minus for a discount" : "0"} />
                </div>
                <p className="min-w-24 pb-2 text-right">
                  <span className="block text-xs text-slate-500">Customer pays</span>
                  <span className="font-mono text-xl font-bold">{formatMoney(linePriceCents(real[i], rates))}</span>
                </p>
              </div>
              {line.ai && (
                <div className="flex flex-wrap items-center gap-2 rounded-xl bg-amber-100 p-2 text-sm font-semibold text-amber-950">
                  <span>⚠️ AI guess at the part cost. Check your supplier&apos;s real price.</span>
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => update(line.key, { ai: false })}>Price is right</button>
                </div>
              )}
            </li>
          ))}
        </ul>
        {lines.length === 0 && <p className="card text-slate-500">No lines yet. Add a part or labor below.</p>}

        {!locked && (
          <div className="grid grid-cols-3 gap-2">
            <button type="button" className="btn btn-secondary" onClick={() => add("part")}>+ Part</button>
            <button type="button" className="btn btn-secondary" onClick={() => add("labor")}>+ Labor</button>
            <button type="button" className="btn btn-secondary" onClick={() => add("fee")}>+ Fee</button>
          </div>
        )}

        <div className="card">
          <label className="label" htmlFor="customer_note">Note to the customer (goes in the email)</label>
          <textarea className="input min-h-24" id="customer_note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} />
        </div>
      </fieldset>

      {aiNotes && aiNotes.assumptions.length > 0 && !isFinal && (
        <div className="card border-amber-200 bg-amber-50">
          <p className="font-bold">What the AI assumed (only you see this)</p>
          <ul className="list-disc space-y-1 pl-5">{aiNotes.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
          <p className="mt-1 text-sm">Confidence in its part prices: <strong>{aiNotes.price_confidence}</strong></p>
        </div>
      )}

      <div className="card space-y-1 text-lg" aria-live="polite">
        <Row label="Parts" cents={totals.parts_cents} />
        <Row label="Labor" cents={totals.labor_cents} />
        {totals.fees_cents !== 0 && <Row label="Fees / discounts" cents={totals.fees_cents} />}
        <Row label={`Tax (${rates.tax_rate_pct}%${rates.tax_labor ? "" : ", parts only"})`} cents={totals.tax_cents} />
        <p className="flex items-baseline justify-between border-t border-slate-200 pt-2 text-2xl font-extrabold">
          <span>{isFinal ? "Final total" : "Quote total"}</span>
          <span className="font-mono" data-testid="quote-total">{formatMoney(totals.total_cents)}</span>
        </p>
        {isFinal && quotedTotalCents !== null && (
          <p className={`text-base font-semibold ${totals.total_cents > quotedTotalCents ? "text-red-700" : "text-green-700"}`}>
            Quoted {formatMoney(quotedTotalCents)} ·{" "}
            {totals.total_cents === quotedTotalCents
              ? "same as the quote"
              : `${formatMoney(Math.abs(totals.total_cents - quotedTotalCents))} ${totals.total_cents > quotedTotalCents ? "over the quote. Check with the customer first." : "under the quote"}`}
          </p>
        )}
        <p className="text-xs text-slate-500">
          Using your rates: {formatMoney(rates.labor_rate_cents)}/hour labor, {rates.parts_markup_pct}% parts markup. Change them in Settings.
        </p>
      </div>

      {result && (
        <p role={result.ok ? "status" : "alert"} className={`rounded-xl p-3 font-medium ${result.ok ? "bg-green-50 text-green-800" : "bg-red-50 text-red-800"}`}>{result.text}</p>
      )}

      {locked ? (
        <form action={reopenQuote.bind(null, ticketId, kind)}>
          <button type="submit" className="btn btn-secondary w-full">Edit this {isFinal ? "final bill" : "quote"} again</button>
        </form>
      ) : (
        <div className="flex flex-col gap-2 sm:flex-row">
          <button type="button" className="btn btn-secondary flex-1" disabled={pending} onClick={() => submit("save")}>{pending ? "Working…" : "Save draft"}</button>
          <button type="button" className="btn btn-primary flex-1" disabled={pending} onClick={() => submit("approve")}>
            {hasCustomerEmail ? "Approve & email customer" : "Approve"}
          </button>
        </div>
      )}
    </div>
  );
}

function Row({ label, cents }: { label: string; cents: number }) {
  return (
    <p className="flex items-baseline justify-between">
      <span className="text-slate-600">{label}</span>
      <span className="font-mono">{formatMoney(cents)}</span>
    </p>
  );
}
