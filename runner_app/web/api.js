// Same-origin local API. Session cookies are HttpOnly; the CSRF value stays in memory.
const ROOT = '/api/v1/';

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message || code || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || 'HTTP_ERROR';
  }
}

export class LocalApi {
  constructor() { this.csrf = null; }

  async request(path, {method = 'GET', body, responseType = 'json'} = {}) {
    if (!/^[-a-zA-Z0-9_/?=&.]+$/.test(path) || path.startsWith('/')) {
      throw new Error('Invalid local API path');
    }
    const headers = {Accept: responseType === 'blob' ? 'application/octet-stream' : 'application/json'};
    if (method !== 'GET') {
      if (!this.csrf && path !== 'local/session') throw new ApiError(403, 'SESSION_REQUIRED');
      headers['Content-Type'] = 'application/json';
      if (this.csrf) headers['X-CSRF-Token'] = this.csrf;
    }
    let response;
    try {
      response = await fetch(ROOT + path, {
        method, credentials: 'same-origin', headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store', redirect: 'error',
      });
    } catch (_) {
      throw new ApiError(0, 'CONNECTION_UNAVAILABLE', 'Локальный сервис недоступен.');
    }
    if (!response.ok) {
      let detail = {};
      try { detail = await response.json(); } catch (_) { /* no trusted error body */ }
      throw new ApiError(response.status, detail.error || detail.code, detail.message);
    }
    if (responseType === 'blob') return response.blob();
    return response.json();
  }

  async resume() {
    const value = await this.request('local/session');
    this.csrf = value.csrf_token;
    return value;
  }

  async login(bootstrap) {
    const value = await this.request('local/session', {method: 'POST', body: {bootstrap}});
    this.csrf = value.csrf_token;
    return value;
  }

  system() { return this.request('system'); }
  projects() { return this.request('projects'); }
  project(id) { return this.request(`projects/${encodeURIComponent(id)}`); }
  workspaces(id) { return this.request(`projects/${encodeURIComponent(id)}/workspaces`); }
  operations(cursor = null) {
    return this.request(`operations?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
  }
  operation(id) { return this.request(`operations/${encodeURIComponent(id)}`); }
  run(id) { return this.request(`runs/${encodeURIComponent(id)}`); }
  outputs(id) { return this.request(`runs/${encodeURIComponent(id)}/outputs`); }
  retention(id) { return this.request(`runs/${encodeURIComponent(id)}/retention`); }
  pinRun(id, pinned) {
    return this.request(`runs/${encodeURIComponent(id)}/retention/pin`, {method: 'POST', body: {pinned}});
  }
  expireOutput(id, outputId) {
    return this.request(`runs/${encodeURIComponent(id)}/outputs/${encodeURIComponent(outputId)}/expire`,
      {method: 'POST', body: {}});
  }
  output(id, outputId) {
    return this.request(`runs/${encodeURIComponent(id)}/outputs/${encodeURIComponent(outputId)}`, {responseType: 'blob'});
  }
  submit(command) { return this.request('operations', {method: 'POST', body: command}); }
  arm() { return this.request('local/arm', {method: 'POST', body: {}}); }
  disarm() { return this.request('local/disarm', {method: 'POST', body: {}}); }
  importHandle(path) { return this.request('local/import-handles', {method: 'POST', body: {path}}); }
  checkDrive() { return this.request('local/check-drive', {method: 'POST', body: {}}); }
  checkGithub() { return this.request('local/check-github', {method: 'POST', body: {}}); }
  checkGitRemote(projectId, workspaceId) {
    return this.request('local/check-git-remote', {method: 'POST', body: {project_id: projectId, workspace_id: workspaceId}});
  }
  workspaceSource(projectId, workspaceId) {
    return this.request(`projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/source`);
  }
  driveDocs(projectId, workspaceId) {
    return this.request(`projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/drive-docs`);
  }
  workspaceConnection(projectId, workspaceId) {
    return this.request(`projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/connections`);
  }
  openEditor(projectId, workspaceId) {
    return this.request('local/open-editor', {method: 'POST', body: {project_id: projectId, workspace_id: workspaceId}});
  }
}
