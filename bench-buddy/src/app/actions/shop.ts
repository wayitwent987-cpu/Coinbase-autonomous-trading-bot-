"use server";

import { randomBytes } from "node:crypto";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { dollarsToCents } from "@/lib/quote-math";
import { getUser, requireShop, slugify } from "@/lib/shop";
import { createAdminClient } from "@/lib/supabase/admin";
import { createClient } from "@/lib/supabase/server";

export type ShopFormState = { error?: string; saved?: boolean } | null;

function readShopFields(formData: FormData) {
  const text = (key: string, max: number) => String(formData.get(key) ?? "").trim().slice(0, max);
  const percent = (key: string, max: number) => {
    const n = Number(String(formData.get(key) ?? "").replace(/[%\s]/g, ""));
    return Number.isFinite(n) ? Math.min(max, Math.max(0, n)) : 0;
  };
  return {
    name: text("name", 80),
    email: text("email", 200) || null,
    phone: text("phone", 40) || null,
    labor_rate_cents: Math.max(0, dollarsToCents(formData.get("labor_rate"))),
    diagnostic_fee_cents: Math.max(0, dollarsToCents(formData.get("diagnostic_fee"))),
    parts_markup_pct: percent("parts_markup_pct", 1000),
    tax_rate_pct: percent("tax_rate_pct", 50),
    tax_labor: formData.get("tax_labor") === "on",
  };
}

function validate(fields: ReturnType<typeof readShopFields>): string | null {
  if (!fields.name) return "Enter your shop's name.";
  if (fields.email && !/^\S+@\S+\.\S+$/.test(fields.email)) return "That shop email doesn't look right.";
  if (fields.labor_rate_cents <= 0) return "Enter your hourly labor rate (for example 80).";
  return null;
}

/** First-time setup: creates the shop for the logged-in user. */
export async function createShop(_prev: ShopFormState, formData: FormData): Promise<ShopFormState> {
  const user = await getUser();
  if (!user) redirect("/login");
  const fields = readShopFields(formData);
  const problem = validate(fields);
  if (problem) return { error: problem };

  const supabase = await createClient();
  const base = slugify(fields.name) || "shop";
  // If another shop already has this web address, add a short random ending.
  for (const slug of [base, `${base}-${randomBytes(2).toString("hex")}`, `${base}-${randomBytes(4).toString("hex")}`]) {
    const { error } = await supabase.from("shops").insert({ ...fields, slug, owner_id: user.id });
    if (!error) redirect("/board?welcome=1");
    if (error.code !== "23505") return { error: error.message };
    if (error.message.includes("owner_id")) redirect("/board"); // already has a shop
  }
  return { error: "Couldn't create a web address for that shop name. Try a slightly different name." };
}

export async function updateShop(_prev: ShopFormState, formData: FormData): Promise<ShopFormState> {
  const { shop } = await requireShop();
  const fields = readShopFields(formData);
  const problem = validate(fields);
  if (problem) return { error: problem };

  let logo_path = shop.logo_path;
  const logo = formData.get("logo");
  if (logo instanceof File && logo.size > 0) {
    const ext = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" }[logo.type];
    if (!ext) return { error: "The logo must be a PNG, JPG or WebP image." };
    if (logo.size > 2 * 1024 * 1024) return { error: "That logo is too big. Use an image under 2 MB." };
    const admin = createAdminClient();
    const path = `${shop.id}/logo-${Date.now()}.${ext}`;
    const { error } = await admin.storage.from("shop-logos").upload(path, logo, { contentType: logo.type });
    if (error) return { error: `Couldn't upload the logo: ${error.message}` };
    if (shop.logo_path) await admin.storage.from("shop-logos").remove([shop.logo_path]);
    logo_path = path;
  }

  const supabase = await createClient();
  const { error } = await supabase.from("shops").update({ ...fields, logo_path }).eq("id", shop.id);
  if (error) return { error: error.message };
  revalidatePath("/", "layout");
  return { saved: true };
}
