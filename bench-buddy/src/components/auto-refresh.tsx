"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** While AI work is running, quietly re-check the page every few seconds. */
export function AutoRefresh({ active, everyMs = 2500 }: { active: boolean; everyMs?: number }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => router.refresh(), everyMs);
    return () => clearInterval(timer);
  }, [active, everyMs, router]);
  return null;
}
