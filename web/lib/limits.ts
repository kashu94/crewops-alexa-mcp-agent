// Request limits for the server routes. Next applies no body-size limit to route handlers,
// so each route refuses an oversized request from its declared length before reading it.

export function declaredTooLarge(request: Request, maxBytes: number): boolean {
  const length = Number(request.headers.get("content-length") ?? "0");
  return !Number.isFinite(length) || length > maxBytes;
}

/** At most `max` uses per key in any `windowMs`. In-memory and per instance, so it only stops
 * one user running up Polly charges; it isn't a distributed quota. */
export class RateLimit {
  private uses = new Map<string, number[]>();
  private swept = 0;

  constructor(
    private max: number,
    private windowMs: number,
  ) {}

  allow(key: string, now = Date.now()): boolean {
    // Forget callers whose uses have all left the window, so a stream of new keys (anyone can
    // send a new X-Forwarded-For) can't grow the map without bound.
    if (now - this.swept >= this.windowMs) {
      this.swept = now;
      for (const [k, times] of this.uses) {
        if (now - times[times.length - 1] >= this.windowMs) this.uses.delete(k);
      }
    }
    const recent = (this.uses.get(key) ?? []).filter((t) => now - t < this.windowMs);
    if (recent.length >= this.max) {
      this.uses.set(key, recent);
      return false;
    }
    recent.push(now);
    this.uses.set(key, recent);
    return true;
  }
}
