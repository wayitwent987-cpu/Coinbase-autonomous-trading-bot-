import type { Metadata } from "next";
import Link from "next/link";
import { createQuote } from "@/app/actions/quotes";
import { deleteTicket, moveTicket, saveNotes, startGuide, startQuoteDraft, startTriage } from "@/app/actions/tickets";
import { AutoRefresh } from "@/components/auto-refresh";
import { ConfirmButton } from "@/components/confirm-button";
import { SafetyBanner } from "@/components/safety-banner";
import { SubmitButton } from "@/components/submit-button";
import { ErrorNote, Working } from "@/components/working";
import { jobView } from "@/lib/ai/job-state";
import { timeAgo } from "@/lib/format";
import { formatMoney } from "@/lib/quote-math";
import { mergeFlags } from "@/lib/safety";
import { createAdminClient } from "@/lib/supabase/admin";
import { nextStatus, STATUSES, STATUS_LABEL } from "@/lib/types";
import { loadTicket } from "./data";

export const metadata: Metadata = { title: "Ticket" };

const DIFFICULTY_LABEL = { easy: "Easy", moderate: "Moderate", hard: "Hard", specialist: "Specialist / send out" } as const;
const CONFIDENCE_STYLE = { high: "bg-green-100 text-green-900", medium: "bg-amber-100 text-amber-900", low: "bg-red-100 text-red-900" } as const;

export default async function TicketPage({ params }: PageProps<"/tickets/[id]">) {
  const { id } = await params;
  const { ticket, supabase, job, quote, final } = await loadTicket(id);

  const [{ data: photoRows }, { data: emails }] = await Promise.all([
    supabase.from("ticket_photos").select("path").eq("ticket_id", id).order("created_at"),
    supabase.from("email_log").select("id, subject, status, detail, created_at").eq("ticket_id", id).order("created_at", { ascending: false }).limit(6),
  ]);
  // Photos are private. Make links that work for one hour, only for photos the
  // security rules just confirmed belong to this shop.
  const paths = (photoRows ?? []).map((p) => p.path);
  const { data: signed } = paths.length
    ? await createAdminClient().storage.from("ticket-photos").createSignedUrls(paths, 3600)
    : { data: [] };
  const photoUrls = (signed ?? []).map((s) => s.signedUrl).filter((url): url is string => Boolean(url));

  const triageJob = jobView(job("triage"));
  const guideJob = jobView(job("guide"));
  const quoteJob = jobView(job("quote"));
  const triage = ticket.triage;
  const flags = mergeFlags(ticket.safety_flags, triage?.safety_flags);
  const next = nextStatus(ticket.status);
  const defaultRepair = triage?.causes[0]?.fix ?? "";

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <AutoRefresh active={[triageJob, guideJob, quoteJob].some((j) => j.state === "running")} />
      <Link href={`/board?s=${ticket.status}`} className="text-sm font-semibold text-blue-700">← Back to board</Link>

      {/* Who, what, where it's at */}
      <header className="card space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="font-mono text-sm font-bold text-slate-500">#{ticket.number} · {ticket.device_type} · {timeAgo(ticket.created_at)}</p>
            <h1 className="text-2xl leading-tight font-bold">{ticket.device_model}</h1>
          </div>
          <span className="rounded-full bg-blue-100 px-3 py-1 text-sm font-bold text-blue-900">{STATUS_LABEL[ticket.status]}</span>
        </div>
        <p className="text-lg">{ticket.customer_name}</p>
        <div className="flex flex-wrap gap-2">
          {ticket.customer_phone && <a className="btn btn-secondary btn-sm" href={`tel:${ticket.customer_phone}`}>📞 {ticket.customer_phone}</a>}
          {ticket.customer_email && <a className="btn btn-secondary btn-sm" href={`mailto:${ticket.customer_email}`}>✉️ {ticket.customer_email}</a>}
        </div>
        <form action={moveTicket.bind(null, ticket.id)} className="flex flex-wrap items-end gap-2 border-t border-slate-100 pt-3">
          {next && (
            <SubmitButton name="status" value={next} className="btn btn-primary flex-1" pendingText="Moving…">
              Move to {STATUS_LABEL[next]} →
            </SubmitButton>
          )}
          <details className="w-full sm:w-auto">
            <summary className="btn btn-secondary btn-sm">Other column…</summary>
            <div className="mt-2 flex flex-wrap gap-2">
              {STATUSES.filter((s) => s !== ticket.status && s !== next).map((s) => (
                <SubmitButton key={s} name="status" value={s} className="btn btn-secondary btn-sm" pendingText="…">{STATUS_LABEL[s]}</SubmitButton>
              ))}
            </div>
          </details>
          <p className="w-full text-xs text-slate-500">Moving emails the customer automatically.</p>
        </form>
      </header>

      <SafetyBanner flags={flags} />

      <section className="card space-y-3">
        <h2 className="card-title">Customer says</h2>
        <p className="text-lg whitespace-pre-wrap">{ticket.problem}</p>
        {photoUrls.length > 0 && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {photoUrls.map((url) => (
              <a key={url} href={url} target="_blank" rel="noreferrer">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={url} alt="Customer photo of the device" className="aspect-square w-full rounded-xl border border-slate-200 object-cover" loading="lazy" />
              </a>
            ))}
          </div>
        )}
      </section>

      {/* 1. Triage */}
      <section className="card space-y-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="card-title">AI triage</h2>
          {triage && <span className={`rounded-full px-3 py-1 text-xs font-bold ${CONFIDENCE_STYLE[triage.confidence]}`}>{triage.confidence} confidence</span>}
        </div>
        {triageJob.state === "running" && <Working>Working out likely causes…</Working>}
        {triageJob.state === "error" && <ErrorNote>{triageJob.error}</ErrorNote>}
        {triage && triageJob.state !== "running" && (
          <>
            <p className="text-lg font-semibold">{triage.summary}</p>
            {triage.uncertain_about && (
              <p className="rounded-xl bg-amber-50 p-3 text-amber-950"><strong>Not sure about:</strong> {triage.uncertain_about}</p>
            )}
            <ol className="space-y-3">
              {triage.causes.map((cause, i) => (
                <li key={i} className="rounded-xl border border-slate-200 p-3">
                  <div className="flex items-baseline justify-between gap-3">
                    <p className="text-lg font-bold">{i + 1}. {cause.title}</p>
                    <p className="shrink-0 font-mono text-lg font-bold text-blue-800">{cause.probability_pct}%</p>
                  </div>
                  <div className="my-2 h-2 overflow-hidden rounded-full bg-slate-200" aria-hidden="true">
                    <div className="h-full rounded-full bg-blue-700" style={{ width: `${cause.probability_pct}%` }} />
                  </div>
                  <p className="text-slate-700"><strong>Why:</strong> {cause.why}</p>
                  <p className="text-slate-700"><strong>Fix:</strong> {cause.fix}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <form action={startGuide.bind(null, ticket.id)}>
                      <input type="hidden" name="repair" value={cause.fix} />
                      <SubmitButton className="btn btn-secondary btn-sm" pendingText="Starting…">🛠 Repair guide</SubmitButton>
                    </form>
                    <form action={startQuoteDraft.bind(null, ticket.id)}>
                      <input type="hidden" name="repair" value={cause.fix} />
                      <SubmitButton className="btn btn-secondary btn-sm" pendingText="Starting…">💲 Draft quote</SubmitButton>
                    </form>
                  </div>
                </li>
              ))}
            </ol>
            <div className="grid gap-4 sm:grid-cols-2">
              <List title="Test first" items={triage.tests_to_run} />
              <List title="Ask the customer" items={triage.questions_to_ask} />
              <List title="Parts to check stock" items={triage.likely_parts} />
              <div>
                <h3 className="font-bold">Difficulty</h3>
                <p><strong>{DIFFICULTY_LABEL[triage.difficulty]}</strong>{triage.difficulty_note ? ` · ${triage.difficulty_note}` : ""}</p>
              </div>
            </div>
          </>
        )}
        {triageJob.state !== "running" && (
          <form action={startTriage.bind(null, ticket.id)} className="flex flex-wrap items-center gap-3">
            <SubmitButton className={triage ? "btn btn-secondary btn-sm" : "btn btn-primary"} pendingText="Starting…">
              {triage ? "Run triage again" : "Run triage"}
            </SubmitButton>
            {triageJob.state === "done" && triageJob.seconds != null && <span className="text-xs text-slate-500">Took {triageJob.seconds}s</span>}
          </form>
        )}
        <p className="text-xs text-slate-500">AI suggestions to speed you up, not a diagnosis. You&apos;re the tech: trust what you see on the bench.</p>
      </section>

      {/* 2. Repair guide */}
      <section className="card space-y-3">
        <h2 className="card-title">Repair guide</h2>
        {guideJob.state === "running" && <Working>Researching and writing the guide… about a minute.</Working>}
        {guideJob.state === "error" && <ErrorNote>{guideJob.error}</ErrorNote>}
        {ticket.guide && guideJob.state !== "running" && (
          <Link href={`/tickets/${ticket.id}/guide`} className="block rounded-xl border border-slate-200 p-3 hover:bg-slate-50">
            <p className="text-lg font-bold">{ticket.guide.title}</p>
            <p className="text-sm text-slate-600">{ticket.guide.steps.length} steps · about {ticket.guide.est_minutes} min · {ticket.guide.confidence} confidence</p>
            <p className="mt-1 font-semibold text-blue-700">Open guide →</p>
          </Link>
        )}
        {guideJob.state !== "running" && (
          <form action={startGuide.bind(null, ticket.id)} className="flex flex-col gap-2 sm:flex-row">
            <input className="input" name="repair" required maxLength={300} defaultValue={ticket.guide?.for_repair ?? defaultRepair} placeholder="What repair? e.g. Replace charging port" aria-label="Repair to build a guide for" />
            <SubmitButton className="btn btn-primary shrink-0" pendingText="Starting…">{ticket.guide ? "Build new guide" : "Build repair guide"}</SubmitButton>
          </form>
        )}
      </section>

      {/* 3. Quote and 4. final price */}
      <section className="card space-y-3">
        <h2 className="card-title">Quote</h2>
        {quoteJob.state === "running" && <Working>Drafting the quote…</Working>}
        {quoteJob.state === "error" && <ErrorNote>{quoteJob.error}</ErrorNote>}
        {quote && quoteJob.state !== "running" && (
          <Link href={`/tickets/${ticket.id}/quote`} className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 p-3 hover:bg-slate-50">
            <span>
              <span className="block text-lg font-bold">{quote.repair_title || "Quote"}</span>
              <span className="text-sm text-slate-600">{quote.status === "draft" ? "Draft: not sent yet. Tap to review." : quote.status === "sent" ? "Approved and emailed" : "Approved"}</span>
            </span>
            <span className="font-mono text-2xl font-bold">{formatMoney(quote.total_cents)}</span>
          </Link>
        )}
        {quoteJob.state !== "running" && (
          <div className="space-y-2">
            <form action={startQuoteDraft.bind(null, ticket.id)} className="flex flex-col gap-2 sm:flex-row">
              <input className="input" name="repair" required maxLength={300} defaultValue={quote ? "" : defaultRepair} placeholder="What repair? e.g. Screen replacement" aria-label="Repair to quote" />
              <SubmitButton className="btn btn-primary shrink-0" pendingText="Starting…">{quote ? "Redo with AI" : "Draft quote with AI"}</SubmitButton>
            </form>
            {!quote && (
              <form action={createQuote.bind(null, ticket.id, "quote")}>
                <SubmitButton className="btn btn-secondary btn-sm" pendingText="Opening…">Or write a quote by hand</SubmitButton>
              </form>
            )}
          </div>
        )}

        <div className="border-t border-slate-100 pt-3">
          <h2 className="card-title">Final price</h2>
          {final ? (
            <Link href={`/tickets/${ticket.id}/quote?kind=final`} className="mt-2 flex items-center justify-between gap-3 rounded-xl border border-slate-200 p-3 hover:bg-slate-50">
              <span className="text-sm text-slate-600">{final.status === "draft" ? "Draft: tap to finish." : final.status === "sent" ? "Final bill emailed" : "Final bill approved"}</span>
              <span className="font-mono text-2xl font-bold">{formatMoney(final.total_cents)}</span>
            </Link>
          ) : (
            <form action={createQuote.bind(null, ticket.id, "final")} className="mt-2 space-y-1">
              <SubmitButton className="btn btn-secondary" pendingText="Opening…">Work out the final price</SubmitButton>
              <p className="text-xs text-slate-500">When the repair is done: starts from the quote, then you enter what you actually used.</p>
            </form>
          )}
        </div>
      </section>

      <section className="card space-y-3">
        <h2 className="card-title">Your notes</h2>
        <form action={saveNotes.bind(null, ticket.id)} className="space-y-2">
          <textarea className="input min-h-28" name="tech_notes" defaultValue={ticket.tech_notes} maxLength={5000} placeholder="What you found, tested, or promised. The AI reads these too." aria-label="Technician notes" />
          <SubmitButton className="btn btn-secondary" pendingText="Saving…">Save notes</SubmitButton>
        </form>
      </section>

      <section className="card space-y-2">
        <h2 className="card-title">Emails to the customer</h2>
        {(emails ?? []).length === 0 && <p className="text-slate-500">None yet.</p>}
        <ul className="divide-y divide-slate-100">
          {(emails ?? []).map((email) => (
            <li key={email.id} className="py-2 text-sm">
              <span className={`mr-2 rounded px-1.5 py-0.5 text-xs font-bold ${email.status === "sent" ? "bg-green-100 text-green-900" : email.status === "error" ? "bg-red-100 text-red-900" : "bg-slate-200 text-slate-700"}`}>
                {email.status === "sent" ? "Sent" : email.status === "error" ? "Failed" : "Not sent"}
              </span>
              {email.subject} <span className="text-slate-500">· {timeAgo(email.created_at)}</span>
              {email.detail && <span className="block text-xs text-slate-500">{email.detail}</span>}
            </li>
          ))}
        </ul>
        <p className="text-xs text-slate-500">
          Customer&apos;s status link: <Link className="text-blue-700 underline" href={`/t/${ticket.public_token}`} target="_blank">open</Link>
        </p>
      </section>

      <form action={deleteTicket.bind(null, ticket.id)} className="pb-8 text-center">
        <ConfirmButton message={`Delete ticket #${ticket.number} and its photos for good?`} className="btn btn-danger btn-sm">Delete this ticket</ConfirmButton>
      </form>
    </div>
  );
}

function List({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <h3 className="font-bold">{title}</h3>
      <ul className="list-disc space-y-1 pl-5">
        {items.map((item, i) => <li key={i}>{item}</li>)}
      </ul>
    </div>
  );
}
