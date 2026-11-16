import type { Metadata } from "next";
import QRCode from "qrcode";
import { logOut } from "@/app/actions/auth";
import { updateShop } from "@/app/actions/shop";
import { CopyButton } from "@/components/copy-button";
import { ShopForm } from "@/components/shop-form";
import { logoUrl, requireShop, siteUrl } from "@/lib/shop";

export const metadata: Metadata = { title: "Settings" };

export default async function SettingsPage() {
  const { shop } = await requireShop();
  const intakeLink = `${siteUrl()}/intake/${shop.slug}`;
  const qr = await QRCode.toDataURL(intakeLink, { width: 480, margin: 2 });
  const logo = logoUrl(shop);

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <h1 className="text-2xl font-bold">Settings</h1>

      <section className="card space-y-4">
        <div>
          <h2 className="card-title">Your customer intake link</h2>
          <p className="text-slate-600">Share this link, or print the QR code and put it on your counter. Customers scan it to check in their own device.</p>
        </div>
        <p className="rounded-xl bg-slate-100 p-3 font-mono text-sm break-all">{intakeLink}</p>
        <div className="flex flex-wrap gap-2">
          <CopyButton text={intakeLink} />
          <a className="btn btn-secondary" href={`/intake/${shop.slug}`} target="_blank">Open intake page</a>
          <a className="btn btn-secondary" href={qr} download={`${shop.slug}-intake-qr.png`}>Download QR code</a>
        </div>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={qr} alt="QR code for your intake page" width={240} height={240} className="rounded-xl border border-slate-200" />
      </section>

      <section className="card space-y-4">
        <h2 className="card-title">Shop details and prices</h2>
        {logo && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={logo} alt={`${shop.name} logo`} className="h-16 w-auto rounded-lg border border-slate-200 bg-white object-contain p-1" />
        )}
        <ShopForm action={updateShop} defaults={shop} submitLabel="Save settings" showLogo />
      </section>

      <form action={logOut} className="pb-8">
        <button className="btn btn-secondary w-full" type="submit">Log out</button>
      </form>
    </div>
  );
}
