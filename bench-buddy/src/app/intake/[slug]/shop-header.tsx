import { logoUrl } from "@/lib/shop";

export function ShopHeader({ shop }: { shop: { name: string; logo_path: string | null } }) {
  const logo = logoUrl(shop);
  return (
    <div className="flex items-center gap-3">
      {logo && (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={logo} alt="" className="h-14 w-14 rounded-xl border border-slate-200 bg-white object-contain p-1" />
      )}
      <p className="text-xl font-bold">{shop.name}</p>
    </div>
  );
}
