"use client";

import Link from "next/link";
import { useActionState } from "react";
import type { AuthState } from "@/app/actions/auth";
import { SubmitButton } from "./submit-button";

export function AuthForm({
  mode,
  action,
}: {
  mode: "login" | "signup";
  action: (prev: AuthState, formData: FormData) => Promise<AuthState>;
}) {
  const [state, formAction] = useActionState(action, null);
  const isSignup = mode === "signup";
  return (
    <form action={formAction} className="card space-y-4">
      <h1 className="text-2xl font-bold">{isSignup ? "Create your shop account" : "Log in"}</h1>
      {state?.error && (
        <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm font-medium text-red-800">
          {state.error}
        </p>
      )}
      {state?.message && (
        <p role="status" className="rounded-xl bg-green-50 p-3 text-sm font-medium text-green-800">
          {state.message}
        </p>
      )}
      <div>
        <label className="label" htmlFor="email">Email</label>
        <input className="input" id="email" name="email" type="email" autoComplete="email" inputMode="email" required />
      </div>
      <div>
        <label className="label" htmlFor="password">Password</label>
        <input
          className="input"
          id="password"
          name="password"
          type="password"
          autoComplete={isSignup ? "new-password" : "current-password"}
          minLength={isSignup ? 8 : undefined}
          required
        />
        {isSignup && <p className="hint">At least 8 characters.</p>}
      </div>
      <SubmitButton className="btn btn-primary w-full">{isSignup ? "Create account" : "Log in"}</SubmitButton>
      <p className="text-center text-sm text-slate-600">
        {isSignup ? (
          <>Already have an account? <Link className="font-semibold text-blue-700 underline" href="/login">Log in</Link></>
        ) : (
          <>New here? <Link className="font-semibold text-blue-700 underline" href="/signup">Create a shop account</Link></>
        )}
      </p>
    </form>
  );
}
