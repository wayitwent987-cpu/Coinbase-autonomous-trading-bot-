import { describe, expect, it } from "vitest";
import { keywordSafetyFlags, mergeFlags } from "../src/lib/safety";

const types = (text: string) => keywordSafetyFlags(text).map((f) => f.type);

describe("keyword safety net", () => {
  it("flags swollen batteries as STOP", () => {
    for (const text of ["battery is swollen", "the back is bulging out", "screen is lifting off the frame", "it got very hot and started smoking", "phone looks puffy"]) {
      const flags = keywordSafetyFlags(text);
      expect(flags.find((f) => f.type === "swollen_battery")?.severity, text).toBe("stop");
    }
  });

  it("flags liquid damage", () => {
    for (const text of ["dropped it in the toilet", "spilled coffee on the keyboard", "it got wet in the rain", "fell in the pool"]) {
      expect(types(text), text).toContain("liquid_damage");
    }
  });

  it("flags data-loss risk", () => {
    for (const text of ["won't turn on and I need my photos", "hard drive is clicking", "I never backed up", "stuck in a boot loop"]) {
      expect(types(text), text).toContain("data_loss_risk");
    }
  });

  it("stays quiet for ordinary faults", () => {
    expect(types("cracked screen, touch still works")).toEqual([]);
    expect(types("left joystick drifts")).toEqual([]);
    expect(types("charging port is loose")).toEqual([]);
  });

  it("can raise several flags at once", () => {
    expect(types("dropped in water, now the battery is swollen and it won't turn on")).toEqual(["swollen_battery", "liquid_damage", "data_loss_risk"]);
  });

  it("never lets the AI downgrade a STOP", () => {
    const merged = mergeFlags(keywordSafetyFlags("battery is swollen"), [{ type: "swollen_battery", severity: "caution", note: "AI note" }]);
    expect(merged).toEqual([{ type: "swollen_battery", severity: "stop", note: "AI note" }]);
  });

  it("adds AI-only flags and puts STOP first", () => {
    const merged = mergeFlags(keywordSafetyFlags("spilled water"), [{ type: "swollen_battery", severity: "stop", note: "Bulge visible in photo" }]);
    expect(merged.map((f) => f.type)).toEqual(["swollen_battery", "liquid_damage"]);
  });
});
