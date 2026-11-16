"use client";

import { useActionState } from "react";
import type { ShopFormState } from "@/app/actions/shop";
import { centsToDollars } from "@/lib/quote-math";
import type { Shop } from "@/lib/types";
import { SubmitButton } from "./submit-button";

type Defaults = Partial<Pick<Shop, "name" | "email" | "phone" | "labor_rate_cents" | "diagnostic_fee_cents" | "parts_markup_pct" | "tax_rate_pct" | "tax_labor">>;

/** Used for both first-time setup and the settings page. */
export function ShopForm({
  action,
  defaults,
  submitLabel,
  showLogo = false,
}: {
  action: (prev: ShopFormState, formData: FormData) => Promise<ShopFormState>;
  defaults: Defaults;
  submitLabel: string;
  showLogo?: boolean;
}) {
  const [state, formAction] = useActionState(action, null);
  return (
    <form action={formAction} className="space-y-4">
      <div>
        <label className="label" htmlFor="name">Shop name</label>
        <input className="input" id="name" name="name" defaultValue={defaults.name ?? ""} maxLength={80} required placeholder="Joe's Phone Repair" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="email">Shop email</label>
          <input className="input" id="email" name="email" type="email" inputMode="email" defaultValue={defaults.email ?? ""} />
          <p className="hint">Customer replies come here.</p>
        </div>
        <div>
          <label className="label" htmlFor="phone">Shop phone <span className="font-normal text-slate-500">(optional)</span></label>
          <input className="input" id="phone" name="phone" type="tel" inputMode="tel" defaultValue={defaults.phone ?? ""} />
        </div>
      </div>

      <fieldset className="space-y-4 rounded-2xl bg-slate-50 p-4">
        <legend className="px-1 text-sm font-bold text-slate-700">Your prices (used for every quote)</legend>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="label" htmlFor="labor_rate">Labor, $ per hour</label>
            <input className="input" id="labor_rate" name="labor_rate" inputMode="decimal" required defaultValue={centsToDollars(defaults.labor_rate_cents ?? 8000)} />
          </div>
          <div>
            <label className="label" htmlFor="parts_markup_pct">Parts markup, %</label>
            <input className="input" id="parts_markup_pct" name="parts_markup_pct" inputMode="decimal" defaultValue={String(defaults.parts_markup_pct ?? 30)} />
          </div>
          <div>
            <label className="label" htmlFor="diagnostic_fee">Diagnostic fee, $</label>
            <input className="input" id="diagnostic_fee" name="diagnostic_fee" inputMode="decimal" defaultValue={centsToDollars(defaults.diagnostic_fee_cents ?? 0)} />
          </div>
          <div>
            <label className="label" htmlFor="tax_rate_pct">Sales tax, %</label>
            <input className="input" id="tax_rate_pct" name="tax_rate_pct" inputMode="decimal" defaultValue={String(defaults.tax_rate_pct ?? 0)} />
          </div>
        </div>
        <label className="flex min-h-12 items-center gap-3 text-base">
          <input type="checkbox" name="tax_labor" defaultChecked={defaults.tax_labor ?? false} className="h-6 w-6 rounded" />
          Charge tax on labor and fees too (not just parts)
        </label>
        <p className="hint">
          Example: a part that costs you $40 with a 30% markup is quoted at $52. Use 0 for anything you don&apos;t charge.
        </p>
      </fieldset>

      {showLogo && (
        <div>
          <label className="label" htmlFor="logo">Shop logo <span className="font-normal text-slate-500">(optional, shown on your intake page)</span></label>
          <input className="input py-2.5" id="logo" name="logo" type="file" accept="image/png,image/jpeg,image/webp" />
        </div>
      )}

      {state?.error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm font-medium text-red-800">{state.error}</p>}
      {state?.saved && <p role="status" className="rounded-xl bg-green-50 p-3 text-sm font-medium text-green-800">Saved.</p>}
      <SubmitButton className="btn btn-primary w-full" pendingText="Saving…">{submitLabel}</SubmitButton>
    </form>
  );
}
