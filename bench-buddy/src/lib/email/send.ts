import "server-only";
import { Resend } from "resend";
import { siteUrl } from "../shop";
import { createAdminClient } from "../supabase/admin";
import type { EmailContext } from "./templates";

type ShopInfo = { id: string; name: string; email: string | null; phone: string | null };
type TicketInfo = {
  id: string;
  number: number;
  public_token: string;
  customer_name: string;
  customer_email: string | null;
  device_model: string;
};

/** Gathers the facts every email template needs. */
export async function buildEmailContext(shop: ShopInfo, ticket: TicketInfo): Promise<EmailContext> {
  const admin = createAdminClient();
  // Only prices the tech has approved are ever shown to a customer.
  const { data: quotes } = await admin
    .from("quotes")
    .select("kind, status, total_cents")
    .eq("ticket_id", ticket.id)
    .neq("status", "draft");
  const final = quotes?.find((q) => q.kind === "final");
  const quote = quotes?.find((q) => q.kind === "quote");
  return {
    shopName: shop.name,
    shopPhone: shop.phone,
    customerName: ticket.customer_name,
    device: ticket.device_model,
    ticketNumber: ticket.number,
    statusLink: `${siteUrl()}/t/${ticket.public_token}`,
    totalCents: final?.total_cents ?? quote?.total_cents ?? null,
    totalIsFinal: Boolean(final),
  };
}

function escapeHtml(text: string) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * Sends one email to the customer and records it in the email log.
 * Never throws: a failed email must not break moving a ticket.
 */
export async function sendCustomerEmail(
  shop: ShopInfo,
  ticket: TicketInfo,
  kind: string,
  email: { subject: string; body: string },
): Promise<"sent" | "skipped" | "error"> {
  const admin = createAdminClient();
  const log = async (status: "sent" | "skipped" | "error", detail: string | null) => {
    await admin.from("email_log").insert({
      shop_id: shop.id,
      ticket_id: ticket.id,
      kind,
      to_email: ticket.customer_email ?? "",
      subject: email.subject,
      status,
      detail,
    });
    return status;
  };

  if (!ticket.customer_email) return log("skipped", "Customer has no email address");
  if (!process.env.RESEND_API_KEY) return log("skipped", "Email isn't set up yet (no RESEND_API_KEY)");

  const fromAddress = process.env.EMAIL_FROM || "onboarding@resend.dev";
  const fromName = shop.name.replace(/[<>"\r\n]/g, "");
  try {
    const resend = new Resend(process.env.RESEND_API_KEY);
    const html = `<div style="font-family:system-ui,sans-serif;font-size:16px;line-height:1.5;white-space:pre-wrap">${escapeHtml(email.body)}</div>`;
    const { error } = await resend.emails.send({
      from: `${fromName} <${fromAddress}>`,
      to: ticket.customer_email,
      replyTo: shop.email ?? undefined,
      subject: email.subject,
      text: email.body,
      html,
    });
    if (error) return log("error", error.message);
    return log("sent", null);
  } catch (error) {
    return log("error", error instanceof Error ? error.message : "Unknown error");
  }
}
