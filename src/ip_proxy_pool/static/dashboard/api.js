export class DashboardApiError extends Error {
  constructor(status, retryAfter = null) {
    const labels = {
      401: "认证失败",
      429: "请求过于频繁",
      503: "服务暂不可用",
    };
    super(labels[status] || `请求失败 (${status})`);
    this.name = "DashboardApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

export class DashboardApi {
  constructor(key) {
    this.key = key;
    this.historyCache = new Map();
    this.historyEtags = new Map();
  }

  async request(path, { signal, etag = null } = {}) {
    const headers = { "X-API-Key": this.key, Accept: "application/json" };
    if (etag) headers["If-None-Match"] = etag;
    const response = await fetch(path, { headers, signal, credentials: "same-origin" });
    if (response.status === 304) return response;
    if (!response.ok) {
      const retryAfter = response.status === 429
        ? Number.parseInt(response.headers.get("Retry-After") || "0", 10)
        : null;
      throw new DashboardApiError(response.status, retryAfter);
    }
    return response;
  }

  async json(path, options = {}) {
    const response = await this.request(path, options);
    return response.json();
  }

  domains(signal) {
    return this.json("/v1/domains", { signal });
  }

  summary(domain, signal) {
    return this.json(this.url("/v1/dashboard/summary", { domain }), { signal });
  }

  quality(domain, signal) {
    return this.json(this.url("/v1/dashboard/quality", { domain }), { signal });
  }

  sources(domain, limit, signal) {
    return this.json(this.url("/v1/dashboard/sources", { domain, limit }), { signal });
  }

  async history(domain, range, signal) {
    const path = this.url("/v1/dashboard/history", { domain, range });
    const response = await this.request(path, {
      signal,
      etag: this.historyEtags.get(path) || null,
    });
    if (response.status === 304) return this.historyCache.get(path);
    const value = await response.json();
    const etag = response.headers.get("ETag");
    if (etag) this.historyEtags.set(path, etag);
    this.historyCache.set(path, value);
    return value;
  }

  proxies(domain, filters, cursor, signal) {
    return this.json(this.url("/v1/proxies", {
      domain,
      state: filters.state,
      min_score: filters.minScore,
      max_score: filters.maxScore,
      source: filters.source,
      limit: 20,
      cursor,
    }), { signal });
  }

  url(path, params) {
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") {
        query.set(name, String(value));
      }
    }
    const encoded = query.toString();
    return encoded ? `${path}?${encoded}` : path;
  }
}
