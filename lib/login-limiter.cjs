// Single-server, bounded failure tracking. Nothing here stores submitted secrets.
class LoginLimiter {
  constructor({ limit = 5, cooldown = 900000, maxSources = 4096, now = Date.now } = {}) {
    Object.assign(this, { limit, cooldown, maxSources, now });
    this.sources = new Map();
  }
  begin(source) {
    const now = this.now();
    for (const [key, value] of this.sources) {
      if (!value.pending && value.expires <= now) this.sources.delete(key);
    }
    let state = this.sources.get(source);
    if (!state) {
      if (this.sources.size >= this.maxSources) return { retryAfter: 60 };
      state = { failures: 0, pending: 0, blocked: 0, expires: now + this.cooldown };
      this.sources.set(source, state);
    }
    if (state.blocked > now) return { retryAfter: Math.ceil((state.blocked - now) / 1000) };
    if (state.failures + state.pending >= this.limit) return { retryAfter: 1 };
    state.pending++;
    let finished = false;
    return {
      finish: (status) => {
        if (finished) return;
        finished = true;
        state.pending--;
        if (status >= 200 && status < 300) state.failures = 0;
        else if (status === 401 || status === 403) {
          state.failures++;
          if (state.failures >= this.limit) state.blocked = this.now() + this.cooldown;
        }
        state.expires = this.now() + this.cooldown;
      },
    };
  }
}
const limiter = new LoginLimiter();
module.exports = { LoginLimiter, limiter };
