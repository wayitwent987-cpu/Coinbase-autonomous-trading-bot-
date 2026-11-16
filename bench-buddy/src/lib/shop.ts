import "server-only";
import { cache } from "react";
import { redirect } from "next/navigation";
import { createClient } from "./supabase/server";
import type { Shop } from "./types";

/** The logged-in user, or null. Cached for the length of one page load. */
export const getUser = cache(async () => {
  const supabase = await createClient();
  const { data } = await supabase.auth.getUser();
  return data.user;
});

/**
 * Use at the top of every private page and action.
 * Not logged in -> login page. Logged in but no shop yet -> setup page.
 */
export const requireShop = cache(async (): Promise<{ shop: Shop; userId: string }> => {
  const user = await getUser();
  if (!user) redirect("/login");
  const supabase = await createClient();
  const { data: shop } = await supabase.from("shops").select("*").maybeSingle();
  if (!shop) redirect("/onboarding");
  return { shop: shop as Shop, userId: user.id };
});

export function siteUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_SITE_URL;
  if (explicit) return explicit.replace(/\/$/, "");
  if (process.env.VERCEL_PROJECT_PRODUCTION_URL) return `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}`;
  if (process.env.VERCEL_URL) return `https://${process.env.VERCEL_URL}`;
  return "http://localhost:3000";
}

export function logoUrl(shop: Pick<Shop, "logo_path">): string | null {
  if (!shop.logo_path) return null;
  return `${process.env.NEXT_PUBLIC_SUPABASE_URL}/storage/v1/object/public/shop-logos/${shop.logo_path}`;
}

export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}
