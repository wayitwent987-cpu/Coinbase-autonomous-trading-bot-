import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { createShop } from "@/app/actions/shop";
import { BenchBuddyLogo } from "@/components/logo";
import { ShopForm } from "@/components/shop-form";
import { getUser } from "@/lib/shop";
import { createClient } from "@/lib/supabase/server";

export const metadata: Metadata = { title: "Set up your shop" };

export default async function OnboardingPage() {
  const user = await getUser();
  if (!user) redirect("/login");
  const supabase = await createClient();
  const { data: existing } = await supabase.from("shops").select("id").maybeSingle();
  if (existing) redirect("/board");

  return (
    <main className="mx-auto w-full max-w-xl flex-1 space-y-5 p-4 py-8">
      <BenchBuddyLogo className="text-xl" />
      <div>
        <h1 className="text-2xl font-bold">Set up your shop</h1>
        <p className="text-slate-600">One minute, one time. You can change any of this later in Settings.</p>
      </div>
      <div className="card">
        <ShopForm action={createShop} defaults={{ email: user.email ?? "" }} submitLabel="Finish setup" />
      </div>
    </main>
  );
}
