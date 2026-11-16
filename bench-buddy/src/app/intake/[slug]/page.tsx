import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { cache } from "react";
import { IntakeForm } from "@/components/intake-form";
import { createAdminClient } from "@/lib/supabase/admin";
import { ShopHeader } from "./shop-header";

// Only the shop's public details are read here. Never tickets.
const getPublicShop = cache(async (slug: string) => {
  const { data } = await createAdminClient().from("shops").select("name, slug, logo_path").eq("slug", slug).maybeSingle();
  return data;
});

export async function generateMetadata({ params }: PageProps<"/intake/[slug]">): Promise<Metadata> {
  const shop = await getPublicShop((await params).slug);
  return { title: shop ? `Repair check-in · ${shop.name}` : "Repair check-in", robots: { index: false } };
}

export default async function IntakePage({ params }: PageProps<"/intake/[slug]">) {
  const shop = await getPublicShop((await params).slug);
  if (!shop) notFound();
  return (
    <>
      <ShopHeader shop={shop} />
      <div>
        <h1 className="text-2xl font-bold">Check in your device</h1>
        <p className="text-slate-600">Takes about a minute. We&apos;ll keep you posted by email.</p>
      </div>
      <div className="card">
        <IntakeForm slug={shop.slug} />
      </div>
      <p className="text-center text-xs text-slate-500">Your details are only shared with {shop.name}.</p>
    </>
  );
}
