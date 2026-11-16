// Safe to use in pages: turns a saved AI job row into what the screen should show.
import type { AiJob } from "../types";

const STALE_MS = 4 * 60 * 1000;

export type JobView = { state: "none" | "running" | "done" | "error"; error?: string; seconds?: number };

export function jobView(job: AiJob | undefined, now = Date.now()): JobView {
  if (!job) return { state: "none" };
  if (job.status === "running") {
    // If the server was interrupted mid-job, don't spin forever.
    if (now - new Date(job.started_at).getTime() > STALE_MS) {
      return { state: "error", error: "That took too long and was stopped. Please try again." };
    }
    return { state: "running" };
  }
  const seconds = job.duration_ms != null ? Math.round(job.duration_ms / 100) / 10 : undefined;
  if (job.status === "error") return { state: "error", error: job.error ?? "Something went wrong.", seconds };
  return { state: "done", seconds };
}
