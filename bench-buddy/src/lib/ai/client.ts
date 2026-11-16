// One small wrapper around the Anthropic API, shared by triage, guides and quotes.
import "server-only";
import Anthropic from "@anthropic-ai/sdk";
import { betaZodOutputFormat } from "@anthropic-ai/sdk/helpers/beta/zod";
import type { z } from "zod";
import { AI_SETTINGS } from "./prompts";

/** An error whose message is safe and useful to show to the tech. */
export class AiError extends Error {}

let cached: Anthropic | null = null;
export function anthropic(): Anthropic {
  if (!process.env.ANTHROPIC_API_KEY) {
    throw new AiError("AI isn't set up yet. Add your ANTHROPIC_API_KEY (see the README), then try again.");
  }
  // 2 minutes max per request, one automatic retry on network hiccups.
  cached ??= new Anthropic({ timeout: 120_000, maxRetries: 1 });
  return cached;
}

// If these models decline a request, Anthropic can automatically re-run it on
// another model instead of failing. Other models don't support that option.
const SUPPORTS_FALLBACK = /^claude-(opus-5|sonnet-5-5|fable-5)/;

export type UserContent = Anthropic.Beta.BetaContentBlockParam[];

/** Ask a question and get back an answer in exactly the shape of `schema`. */
export async function askStructured<S extends z.ZodType>(
  task: keyof typeof AI_SETTINGS,
  system: string,
  content: UserContent,
  schema: S,
): Promise<z.infer<S>> {
  const { model, effort, maxTokens } = AI_SETTINGS[task];
  const useFallback = SUPPORTS_FALLBACK.test(model);
  try {
    const response = await anthropic().beta.messages.parse({
      model,
      max_tokens: maxTokens,
      system,
      messages: [{ role: "user", content }],
      output_config: { effort, format: betaZodOutputFormat(schema) },
      ...(useFallback
        ? { betas: ["server-side-fallback-2026-07-01"], fallbacks: "default" as const }
        : {}),
    });
    if (response.stop_reason === "refusal") {
      throw new AiError("The AI declined to answer this one. Reword the problem description or handle it manually.");
    }
    if (response.stop_reason === "max_tokens") {
      throw new AiError("The AI's answer was cut off. Try again, or raise maxTokens in src/lib/ai/prompts.ts.");
    }
    if (!response.parsed_output) {
      throw new AiError("The AI's answer came back in an unexpected format. Please try again.");
    }
    return response.parsed_output;
  } catch (error) {
    throw toAiError(error);
  }
}

/** Turns technical API errors into something a tech can act on. */
export function toAiError(error: unknown): AiError {
  if (error instanceof AiError) return error;
  if (error instanceof Anthropic.AuthenticationError) {
    return new AiError("Your Anthropic API key was rejected. Check ANTHROPIC_API_KEY.");
  }
  if (error instanceof Anthropic.PermissionDeniedError) {
    return new AiError("Your Anthropic account isn't allowed to use this model or feature.");
  }
  if (error instanceof Anthropic.NotFoundError) {
    return new AiError("The AI model name in src/lib/ai/prompts.ts wasn't recognised.");
  }
  if (error instanceof Anthropic.RateLimitError) {
    return new AiError("The AI is busy (rate limit). Wait a minute and try again.");
  }
  if (error instanceof Anthropic.APIConnectionTimeoutError) {
    return new AiError("The AI took too long to answer. Try again.");
  }
  if (error instanceof Anthropic.APIConnectionError) {
    return new AiError("Couldn't reach the AI service. Check the internet connection and try again.");
  }
  if (error instanceof Anthropic.BadRequestError) {
    console.error("[ai] bad request", error.message);
    return new AiError(`The AI rejected the request: ${error.message}`.slice(0, 300));
  }
  if (error instanceof Anthropic.APIError) {
    return new AiError("The AI service had a problem. Try again in a moment.");
  }
  console.error("[ai] unexpected error", error);
  return new AiError("Something went wrong while talking to the AI. Try again.");
}
