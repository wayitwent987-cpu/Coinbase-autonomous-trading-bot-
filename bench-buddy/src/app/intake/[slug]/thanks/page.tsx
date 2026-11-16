import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { createAdminClient } from "@/lib/supabase/admin";
import { ShopHeader } from "../shop-header";

export const metadata: Metadata = { title: "Thanks!", robots: { index: false } };

// Fixed safety wording shown to customers. Written by people, not AI, and it
// only ever tells the customer to stop using the device and bring it in.
const CUSTOMER_SAFETY: Record<string, string> = {
  swollen_battery:
    "Because you mentioned swelling, heat or smoke: please stop using and charging the device now, don't press on it or try to open it, keep it away from anything that could burn, and bring it to the shop as soon as you can.",
  liquid_damage:
    "Because you mentioned liquid: please leave the device switched off and don't charge it. Bring it in as soon as you can. The sooner we see it, the better the chances.",
};

export default async function ThanksPage({ params, searchParams }: PageProps<"/intake/[slug]/thanks">) {
  const { slug } = await params;
  const query = await searchParams;
  const { data: shop } = await createAdminClient().from("shops").select("name, slug, logo_path, phone").eq("slug", slug).maybeSingle();
  if (!shop) notFound();

  const number = typeof query.n === "string" && /^\d+$/.test(query.n) ? query.n : null;
  const token = typeof query.t === "string" && /^[0-9a-f]{32}$/.test(query.t) ? query.t : null;
  const flags = typeof query.f === "string" ? query.f.split(",") : [];
  const warnings = Object.entries(CUSTOMER_SAFETY).filter(([type]) => flags.includes(type));

  return (
    <>
      <ShopHeader shop={shop} />
      <div className="card space-y-3 text-center">
        <p className="text-5xl" aria-hidden="true">✅</p>
        <h1 className="text-2xl font-bold">Got it, thanks!</h1>
        {number && <p className="text-lg">Your ticket number is <strong className="font-mono text-2xl">#{number}</strong></p>}
        <p className="text-slate-600">{shop.name} will be in touch. If you gave an email, a confirmation is on its way.</p>
        {token && <Link className="btn btn-secondary w-full" href={`/t/${token}`}>Check my repair status</Link>}
      </div>
      {warnings.map(([type, text]) => (
        <div key={type} role="alert" className="rounded-2xl border-2 border-red-300 bg-red-50 p-4 text-red-900">
          <p className="font-bold">⚠️ Important safety note</p>
          <p>{text}</p>
          {shop.phone && <p className="mt-2 font-semibold">Shop phone: <a className="underline" href={`tel:${shop.phone}`}>{shop.phone}</a></p>}
        </div>
      ))}
    </>
  );
}
