"use client";

import { useEffect, useState } from "react";

type Step = { title: string; detail: string; caution: string };

/** The steps as a big tick-off checklist. Ticks are remembered on this device. */
export function GuideSteps({ steps, storageKey }: { steps: Step[]; storageKey: string }) {
  const [done, setDone] = useState<boolean[]>([]);

  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) ?? "[]");
      // eslint-disable-next-line react-hooks/set-state-in-effect -- restore saved ticks after first paint
      if (Array.isArray(saved)) setDone(saved.map(Boolean));
    } catch {
      // No saved progress (or storage is blocked): start fresh.
    }
  }, [storageKey]);

  function toggle(index: number) {
    setDone((prev) => {
      const next = steps.map((_, i) => (i === index ? !prev[i] : Boolean(prev[i])));
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        // Ticks just won't be remembered.
      }
      return next;
    });
  }

  const count = steps.filter((_, i) => done[i]).length;
  return (
    <div className="space-y-3">
      <p className="text-sm font-semibold text-slate-600" role="status">{count} of {steps.length} steps done</p>
      <ol className="space-y-3">
        {steps.map((step, i) => (
          <li key={i} className={`rounded-2xl border-2 p-4 ${done[i] ? "border-green-300 bg-green-50" : "border-slate-200 bg-white"}`}>
            <label className="flex cursor-pointer items-start gap-3">
              <input type="checkbox" checked={Boolean(done[i])} onChange={() => toggle(i)} className="mt-1 h-7 w-7 shrink-0 rounded" />
              <span className="min-w-0">
                <span className={`block text-lg font-bold ${done[i] ? "text-slate-500 line-through" : ""}`}>{i + 1}. {step.title}</span>
                <span className="block text-lg text-slate-800">{step.detail}</span>
              </span>
            </label>
            {step.caution && (
              <p className="mt-3 rounded-xl bg-amber-100 p-3 font-semibold text-amber-950">⚠️ {step.caution}</p>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
