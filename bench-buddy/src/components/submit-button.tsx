"use client";

import { useFormStatus } from "react-dom";

/** A form button that shows "working" and can't be double-tapped. */
export function SubmitButton({
  children,
  pendingText = "Working…",
  className = "btn btn-primary",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { pendingText?: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className={className} disabled={pending || props.disabled} {...props}>
      {pending ? pendingText : children}
    </button>
  );
}
