import { FLAG_LABEL } from "@/lib/safety";
import type { SafetyFlag } from "@/lib/types";

/** Loud, impossible-to-miss safety warnings. Red = stop, amber = take care. */
export function SafetyBanner({ flags }: { flags: SafetyFlag[] }) {
  if (flags.length === 0) return null;
  return (
    <div className="space-y-2" role="alert">
      {flags.map((flag, i) => (
        <div
          key={i}
          className={
            flag.severity === "stop"
              ? "rounded-2xl border-2 border-red-600 bg-red-600 p-4 text-white"
              : "rounded-2xl border-2 border-amber-400 bg-amber-50 p-4 text-amber-950"
          }
        >
          <p className="text-lg font-extrabold uppercase tracking-wide">
            {flag.severity === "stop" ? "⛔ Stop: " : "⚠️ "}
            {FLAG_LABEL[flag.type]}
          </p>
          <p className="text-base font-medium">{flag.note}</p>
        </div>
      ))}
    </div>
  );
}

/** Small badges for board cards. */
export function SafetyBadges({ flags }: { flags: SafetyFlag[] }) {
  if (flags.length === 0) return null;
  return (
    <span className="flex flex-wrap gap-1">
      {flags.map((flag, i) => (
        <span
          key={i}
          className={`rounded-md px-1.5 py-0.5 text-xs font-bold ${flag.severity === "stop" ? "bg-red-600 text-white" : "bg-amber-200 text-amber-950"}`}
        >
          {flag.severity === "stop" ? "⛔" : "⚠️"} {FLAG_LABEL[flag.type]}
        </span>
      ))}
    </span>
  );
}
