const API = {
  async request(method, url, body) {
    const opts = { method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const msg = data && data.detail ? data.detail : (data && data.error ? data.error : `HTTP ${resp.status}`);
      throw new Error(msg);
    }
    return data;
  },
  get(url) { return this.request('GET', url); },
  post(url, body) { return this.request('POST', url, body); },
  put(url, body) { return this.request('PUT', url, body); },
  del(url) { return this.request('DELETE', url); },
  async upload(url, file) {
    const fd = new FormData();
    fd.append('file', file);
    const resp = await fetch(url, { method: 'POST', credentials: 'same-origin', body: fd });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const msg = data && data.detail ? data.detail : (data && data.error ? data.error : `HTTP ${resp.status}`);
      throw new Error(msg);
    }
    return data;
  },
};
