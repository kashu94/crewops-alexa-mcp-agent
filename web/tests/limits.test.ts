import { describe, expect, it } from "vitest";

import { declaredTooLarge, RateLimit } from "@/lib/limits";

describe("request limits", () => {
  it("refuses a body declared larger than the limit", () => {
    const req = (length?: string) =>
      new Request("http://x/api", { method: "POST", headers: length ? { "content-length": length } : {} });
    expect(declaredTooLarge(req("100"), 2000)).toBe(false);
    expect(declaredTooLarge(req("5000"), 2000)).toBe(true);
    expect(declaredTooLarge(req("abc"), 2000)).toBe(true);
    expect(declaredTooLarge(req(), 2000)).toBe(false);
  });

  it("allows a few uses per window and then pauses", () => {
    const limit = new RateLimit(2, 1000);
    expect(limit.allow("u", 0)).toBe(true);
    expect(limit.allow("u", 10)).toBe(true);
    expect(limit.allow("u", 20)).toBe(false);
    expect(limit.allow("other", 20)).toBe(true);
    expect(limit.allow("u", 1011)).toBe(true); // the first use has left the window
  });

  it("forgets callers whose uses have left the window", () => {
    const limit = new RateLimit(1, 1000);
    for (let i = 0; i < 5000; i++) limit.allow(`client-${i}`, 0);
    expect(limit.allow("late", 2000)).toBe(true);
    expect((limit as unknown as { uses: Map<string, number[]> }).uses.size).toBe(1);
    expect(limit.allow("late", 2001)).toBe(false); // the live caller is still limited
  });
});
