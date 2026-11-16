"use server";

import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { after } from "next/server";
import { z } from "zod";
import { beginJob, finishJob } from "@/lib/ai/jobs";
import { runTriage } from "@/lib/ai/triage";
import { buildEmailContext, sendCustomerEmail } from "@/lib/email/send";
import { intakeEmail } from "@/lib/email/templates";
import { keywordSafetyFlags } from "@/lib/safety";
import { createAdminClient } from "@/lib/supabase/admin";

const MAX_PHOTOS = 4;
const MAX_PHOTO_BYTES = 3 * 1024 * 1024;
const PHOTO_TYPES: Record<string, string> = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp" };

const IntakeSchema = z.object({
  customer_name: z.string().trim().min(1, "Please enter your name.").max(100),
  customer_email: z.union([z.literal(""), z.email("That email doesn't look right.")]),
  customer_phone: z.string().trim().max(40),
  device_type: z.string().trim().min(1, "Please choose a device type.").max(40),
  device_model: z.string().trim().min(1, "Please enter the device model.").max(120),
  problem: z.string().trim().min(5, "Please describe the problem in a few words.").max(4000),
});

/**
 * Saves a ticket from the public intake form. The customer is not logged in,
 * so this runs on our server with the server-only key, and checks everything.
 */
export async function submitIntake(slug: string, formData: FormData): Promise<{ error: string }> {
  // Hidden field real people never fill in. Bots do; quietly pretend it worked.
  if (String(formData.get("website") ?? "")) redirect(`/intake/${slug}/thanks`);

  const parsed = IntakeSchema.safeParse({
    customer_name: formData.get("customer_name") ?? "",
    customer_email: String(formData.get("customer_email") ?? "").trim().toLowerCase(),
    customer_phone: formData.get("customer_phone") ?? "",
    device_type: formData.get("device_type") ?? "",
    device_model: formData.get("device_model") ?? "",
    problem: formData.get("problem") ?? "",
  });
  if (!parsed.success) return { error: parsed.error.issues[0].message };
  const fields = parsed.data;
  if (!fields.customer_email && !fields.customer_phone) {
    return { error: "Please give an email or phone number so the shop can reach you." };
  }

  const photos = formData.getAll("photos").filter((f): f is File => f instanceof File && f.size > 0);
  if (photos.length > MAX_PHOTOS) return { error: `Please attach at most ${MAX_PHOTOS} photos.` };
  for (const photo of photos) {
    if (!PHOTO_TYPES[photo.type]) return { error: "Photos must be JPG, PNG or WebP images." };
    if (photo.size > MAX_PHOTO_BYTES) return { error: "One of the photos is too large. Try a smaller one." };
  }

  const admin = createAdminClient();
  const { data: shop } = await admin.from("shops").select("id, name, email, phone").eq("slug", slug).maybeSingle();
  if (!shop) return { error: "This shop's intake page isn't available. Please check the link." };

  const { data: number, error: numberError } = await admin.rpc("take_ticket_number", { p_shop_id: shop.id });
  if (numberError || typeof number !== "number") return { error: "Something went wrong. Please try again." };

  const safety_flags = keywordSafetyFlags(`${fields.problem} ${fields.device_model}`);
  const { data: ticket, error } = await admin
    .from("tickets")
    .insert({
      shop_id: shop.id,
      number,
      ...fields,
      customer_email: fields.customer_email || null,
      customer_phone: fields.customer_phone || null,
      safety_flags,
    })
    .select("id, shop_id, number, public_token, customer_name, customer_email, device_model")
    .single();
  if (error || !ticket) return { error: "Something went wrong saving your request. Please try again." };

  // Photos go in a private folder: <shop>/<ticket>/<random>.jpg
  for (const photo of photos) {
    const path = `${shop.id}/${ticket.id}/${randomUUID()}.${PHOTO_TYPES[photo.type]}`;
    const upload = await admin.storage.from("ticket-photos").upload(path, photo, { contentType: photo.type });
    if (!upload.error) {
      await admin.from("ticket_photos").insert({ ticket_id: ticket.id, shop_id: shop.id, path });
    }
  }

  // The customer sees "thanks" straight away; triage and the confirmation
  // email carry on in the background.
  await beginJob(ticket, "triage").catch(() => {});
  after(async () => {
    await Promise.all([
      finishJob(ticket, "triage", () => runTriage(ticket.id)),
      buildEmailContext(shop, ticket).then((ctx) => sendCustomerEmail(shop, ticket, "intake", intakeEmail(ctx))),
    ]);
  });

  const flags = safety_flags.map((f) => f.type).join(",");
  redirect(`/intake/${slug}/thanks?n=${ticket.number}&t=${ticket.public_token}${flags ? `&f=${flags}` : ""}`);
}
