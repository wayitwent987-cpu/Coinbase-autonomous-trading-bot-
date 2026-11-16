// The exact words customers receive. Edit freely - these are fixed templates,
// NOT written by AI, so a customer never gets repair advice by email.
import { formatMoney } from "../quote-math";
import type { TicketStatus } from "../types";

export type EmailContext = {
  shopName: string;
  shopPhone: string | null;
  customerName: string;
  device: string;
  ticketNumber: number;
  statusLink: string;
  /** Final bill if there is one, otherwise the approved quote. */
  totalCents: number | null;
  totalIsFinal: boolean;
};

type Email = { subject: string; body: string };

function wrap(ctx: EmailContext, lines: string[]): string {
  const first = ctx.customerName.trim().split(/\s+/)[0] || "there";
  return [
    `Hi ${first},`,
    "",
    ...lines,
    "",
    `Check your repair any time: ${ctx.statusLink}`,
    "",
    "Questions? Just reply to this email" + (ctx.shopPhone ? ` or call ${ctx.shopPhone}.` : "."),
    "",
    `- ${ctx.shopName}`,
    `Ticket #${ctx.ticketNumber}`,
  ].join("\n");
}

/** Sent to the customer right after they submit the intake form. */
export function intakeEmail(ctx: EmailContext): Email {
  return {
    subject: `We got your repair request (#${ctx.ticketNumber})`,
    body: wrap(ctx, [
      `Thanks! ${ctx.shopName} has your repair request for your ${ctx.device}.`,
      "We'll take a look and let you know what we find.",
    ]),
  };
}

/** Sent when the tech moves a ticket to a new column. Return null to send nothing. */
export function statusEmail(status: TicketStatus, ctx: EmailContext): Email | null {
  switch (status) {
    case "received":
      return null;
    case "diagnosing":
      return {
        subject: `We're looking at your ${ctx.device}`,
        body: wrap(ctx, [`Good news: your ${ctx.device} is on the bench and we're working out what's wrong.`, "We'll be in touch with what we find."]),
      };
    case "waiting_parts":
      return {
        subject: `Parts ordered for your ${ctx.device}`,
        body: wrap(ctx, [`We've found the problem with your ${ctx.device} and we're waiting on parts.`, "We'll get going as soon as they arrive and let you know when it's ready."]),
      };
    case "ready":
      return {
        subject: `Your ${ctx.device} is ready for pickup`,
        body: wrap(ctx, [
          `Your ${ctx.device} is ready to pick up!`,
          ...(ctx.totalCents !== null
            ? [`${ctx.totalIsFinal ? "Total due" : "Quoted total"}: ${formatMoney(ctx.totalCents)}`]
            : []),
        ]),
      };
    case "picked_up":
      return {
        subject: `Thanks from ${ctx.shopName}`,
        body: wrap(ctx, [`Thanks for trusting us with your ${ctx.device}.`, "If anything doesn't seem right, get in touch and we'll sort it out."]),
      };
  }
}

export type PricedLine = { description: string; priceCents: number };

/** Sent when the tech approves a quote (or the final bill) and taps Send. */
export function quoteEmail(
  ctx: EmailContext,
  q: { isFinal: boolean; repairTitle: string; lines: PricedLine[]; subtotalCents: number; taxCents: number; totalCents: number; note: string },
): Email {
  return {
    subject: q.isFinal
      ? `Your final bill for the ${ctx.device}: ${formatMoney(q.totalCents)}`
      : `Your repair quote for the ${ctx.device}: ${formatMoney(q.totalCents)}`,
    body: wrap(ctx, [
      q.isFinal ? `Here's the final bill for your ${ctx.device}.` : `Here's our quote to repair your ${ctx.device}.`,
      "",
      ...(q.repairTitle ? [q.repairTitle, ""] : []),
      ...q.lines.map((l) => `  ${l.description}: ${formatMoney(l.priceCents)}`),
      ...(q.taxCents ? [`  Tax: ${formatMoney(q.taxCents)}`] : []),
      `  ${q.isFinal ? "TOTAL DUE" : "ESTIMATED TOTAL"}: ${formatMoney(q.totalCents)}`,
      ...(q.note.trim() ? ["", q.note.trim()] : []),
      ...(q.isFinal ? [] : ["", "Reply to this email to approve the repair or ask a question. This is an estimate; if we find anything unexpected we'll check with you first."]),
    ]),
  };
}
