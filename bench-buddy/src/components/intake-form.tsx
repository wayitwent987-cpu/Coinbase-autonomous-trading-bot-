"use client";

import { useState } from "react";
import { submitIntake } from "@/app/actions/intake";
import { DEVICE_TYPES } from "@/lib/types";

const MAX_PHOTOS = 4;

/**
 * Phone cameras make huge photos. Shrink each one in the browser before
 * upload so the form is fast even on slow mobile data.
 */
async function shrinkPhoto(file: File): Promise<File> {
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d")!.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.8));
    if (!blob) return file;
    return new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" });
  } catch {
    return file; // Couldn't shrink (unusual format): send as-is, the server checks it.
  }
}

export function IntakeForm({ slug }: { slug: string }) {
  const [photos, setPhotos] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const formData = new FormData(event.currentTarget);
      formData.delete("photos");
      for (const photo of await Promise.all(photos.map(shrinkPhoto))) formData.append("photos", photo);
      // On success the server sends the customer to the "thanks" page.
      const result = await submitIntake(slug, formData);
      if (result?.error) setError(result.error);
    } catch {
      setError("Couldn't send your request. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div>
        <label className="label" htmlFor="customer_name">Your name</label>
        <input className="input" id="customer_name" name="customer_name" autoComplete="name" maxLength={100} required />
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="customer_email">Email</label>
          <input className="input" id="customer_email" name="customer_email" type="email" inputMode="email" autoComplete="email" />
          <p className="hint">We&apos;ll email you updates on your repair.</p>
        </div>
        <div>
          <label className="label" htmlFor="customer_phone">Phone</label>
          <input className="input" id="customer_phone" name="customer_phone" type="tel" inputMode="tel" autoComplete="tel" maxLength={40} />
        </div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="device_type">What kind of device?</label>
          <select className="input" id="device_type" name="device_type" required defaultValue="">
            <option value="" disabled>Choose…</option>
            {DEVICE_TYPES.map((type) => <option key={type}>{type}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="device_model">Make and model</label>
          <input className="input" id="device_model" name="device_model" maxLength={120} required placeholder="e.g. iPhone 13, PS5, Dell XPS 15" />
        </div>
      </div>
      <div>
        <label className="label" htmlFor="problem">What&apos;s wrong with it?</label>
        <textarea className="input min-h-32" id="problem" name="problem" maxLength={4000} required placeholder="Tell us what happened and what it does now. Did it get dropped or wet?" />
      </div>
      <div>
        <label className="label" htmlFor="photos">Photos <span className="font-normal text-slate-500">(optional, up to {MAX_PHOTOS})</span></label>
        <input
          className="input py-2.5"
          id="photos"
          name="photos"
          type="file"
          accept="image/*"
          multiple
          onChange={(e) => {
            const chosen = Array.from(e.target.files ?? []);
            setPhotos(chosen.slice(0, MAX_PHOTOS));
            setError(chosen.length > MAX_PHOTOS ? `Only the first ${MAX_PHOTOS} photos will be sent.` : null);
          }}
        />
        {photos.length > 0 && <p className="hint">{photos.length} photo{photos.length === 1 ? "" : "s"} attached.</p>}
      </div>
      {/* Spam trap: hidden from people, tempting to bots. */}
      <div className="hidden" aria-hidden="true">
        <label>Website <input name="website" tabIndex={-1} autoComplete="off" /></label>
      </div>
      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm font-medium text-red-800">{error}</p>}
      <button type="submit" className="btn btn-primary w-full text-lg" disabled={busy}>
        {busy ? "Sending…" : "Send to the shop"}
      </button>
    </form>
  );
}
