import type { Metadata } from "next";
import Link from "next/link";
import { startGuide } from "@/app/actions/tickets";
import { AutoRefresh } from "@/components/auto-refresh";
import { GuideSteps } from "@/components/guide-steps";
import { SafetyBanner } from "@/components/safety-banner";
import { SubmitButton } from "@/components/submit-button";
import { ErrorNote, Working } from "@/components/working";
import { jobView } from "@/lib/ai/job-state";
import { mergeFlags } from "@/lib/safety";
import { loadTicket } from "../data";

export const metadata: Metadata = { title: "Repair guide" };

const CONFIDENCE_TEXT = {
  high: "High confidence for this exact model.",
  medium: "Medium confidence: partly based on similar models. Check the details below.",
  low: "Low confidence: mostly general procedure. Find a service manual before opening.",
} as const;

export default async function GuidePage({ params }: PageProps<"/tickets/[id]/guide">) {
  const { id } = await params;
  const { ticket, job } = await loadTicket(id);
  const state = jobView(job("guide"));
  const guide = ticket.guide;
  const flags = mergeFlags(ticket.safety_flags, ticket.triage?.safety_flags);

  return (
    <div className="mx-auto max-w-2xl space-y-4 pb-10">
      <AutoRefresh active={state.state === "running"} />
      <Link href={`/tickets/${ticket.id}`} className="text-sm font-semibold text-blue-700">← Back to ticket #{ticket.number}</Link>

      {state.state === "running" && (
        <div className="card space-y-2">
          <Working>Researching and writing your guide…</Working>
          <p className="text-slate-600">This usually takes about a minute. You can leave this page; it keeps going.</p>
        </div>
      )}
      {state.state === "error" && <ErrorNote>{state.error}</ErrorNote>}
      {!guide && state.state === "none" && <p className="card">No guide yet. Go back to the ticket and tap “Build repair guide”.</p>}

      {guide && state.state !== "running" && (
        <>
          <header className="space-y-1">
            <p className="text-sm font-semibold text-slate-500">{ticket.device_model}</p>
            <h1 className="text-3xl leading-tight font-extrabold">{guide.title}</h1>
            <p className="text-lg text-slate-700">{guide.summary}</p>
            <p className="text-sm text-slate-600">About {guide.est_minutes} min · {guide.steps.length} steps</p>
          </header>

          <SafetyBanner flags={flags} />
          {guide.safety_warnings.length > 0 && (
            <div className="rounded-2xl border-2 border-red-300 bg-red-50 p-4 text-red-950" role="alert">
              <p className="text-lg font-extrabold">⚠️ Safety for this repair</p>
              <ul className="list-disc space-y-1 pl-5 text-lg">{guide.safety_warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
            </div>
          )}

          <div className={`rounded-2xl border-2 p-4 ${guide.confidence === "high" ? "border-green-300 bg-green-50" : "border-amber-300 bg-amber-50"}`}>
            <p className="font-bold">{CONFIDENCE_TEXT[guide.confidence]}</p>
            <p className="text-sm">
              {guide.web_checked ? "Based on a web search (sources at the bottom)." : "Not web-checked: written from the AI's general knowledge."}
            </p>
            {guide.verify_first.length > 0 && (
              <>
                <p className="mt-2 font-bold">Double-check these before you rely on them:</p>
                <ul className="list-disc space-y-1 pl-5">{guide.verify_first.map((v, i) => <li key={i}>{v}</li>)}</ul>
              </>
            )}
          </div>

          <Section title="Before you start" items={guide.before_you_start} />
          <div className="grid gap-4 sm:grid-cols-2">
            <Section title="Tools" items={guide.tools} />
            <Section title="Parts" items={guide.parts} />
          </div>

          <h2 className="pt-2 text-2xl font-bold">Steps</h2>
          <GuideSteps steps={guide.steps} storageKey={`guide:${ticket.id}:${guide.generated_at}`} />

          <Section title="Test before it goes back to the customer" items={guide.test_checklist} />

          {guide.sources.length > 0 && (
            <div className="card">
              <h2 className="card-title">Sources</h2>
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {guide.sources.map((s) => (
                  <li key={s.url}><a className="break-words text-blue-700 underline" href={s.url} target="_blank" rel="noreferrer">{s.title}</a></li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-sm text-slate-500">
            This guide was written by AI for: “{guide.for_repair}”. It can be wrong, especially on model-specific details. If something on the bench doesn&apos;t match, stop and check a service manual.
          </p>
        </>
      )}

      {state.state !== "running" && (
        <form action={startGuide.bind(null, ticket.id)} className="card flex flex-col gap-2 sm:flex-row print:hidden">
          <input className="input" name="repair" required maxLength={300} defaultValue={guide?.for_repair ?? ticket.triage?.causes[0]?.fix ?? ""} placeholder="What repair?" aria-label="Repair to build a guide for" />
          <SubmitButton className="btn btn-secondary shrink-0" pendingText="Starting…">{guide ? "Build a new guide" : "Build repair guide"}</SubmitButton>
        </form>
      )}
    </div>
  );
}

function Section({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="card">
      <h2 className="card-title">{title}</h2>
      <ul className="list-disc space-y-1 pl-5 text-lg">{items.map((item, i) => <li key={i}>{item}</li>)}</ul>
    </div>
  );
}
