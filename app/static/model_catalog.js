// Lazy, provider-keyed model catalog shared by every authoring model picker.
// Successful responses (including an empty offline/non-listable result) live for
// the current browser session. The server-rendered login-session scope keeps
// model names from crossing login or organization boundaries in sessionStorage.
(function (global) {
  'use strict';

  var memory = {};
  var pending = {};
  var storagePrefix = 'loc-model-catalog:v1:';

  function normalizedModels(value) {
    if (!Array.isArray(value)) return [];
    return value.filter(function (model) { return typeof model === 'string'; });
  }

  function cacheKey(ref) {
    var scope;
    try { scope = JSON.stringify(global.LOC_MODEL_CACHE_SCOPE || null); }
    catch (err) { scope = 'null'; }
    return scope + ':' + String(ref);
  }

  function storageKey(key) {
    return storagePrefix + encodeURIComponent(key);
  }

  function readSession(key) {
    try {
      var raw = global.sessionStorage.getItem(storageKey(key));
      if (raw === null) return null;
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? normalizedModels(parsed) : null;
    } catch (err) {
      return null;
    }
  }

  function writeSession(key, models) {
    try { global.sessionStorage.setItem(storageKey(key), JSON.stringify(models)); }
    catch (err) {}
  }

  function get(ref, options) {
    options = options || {};
    ref = String(ref || '').trim();
    if (!ref) return Promise.resolve([]);

    var key = cacheKey(ref);
    // A refresh already in flight is newer than either cache tier. Join it
    // before reading stale memory/session data so a reopen renders its result.
    if (pending[key]) {
      var inflight = pending[key];
      if (options.refresh) return inflight;
      // Reopens may use the last good catalog if someone else's refresh fails.
      return inflight.catch(function (err) {
        if (Object.prototype.hasOwnProperty.call(memory, key)) return memory[key].slice();
        var fallback = readSession(key);
        if (fallback !== null) { memory[key] = fallback; return fallback.slice(); }
        throw err;
      });
    }
    if (!options.refresh) {
      if (Object.prototype.hasOwnProperty.call(memory, key)) {
        return Promise.resolve(memory[key].slice());
      }
      var stored = readSession(key);
      if (stored !== null) {
        memory[key] = stored;
        return Promise.resolve(stored.slice());
      }
    }
    pending[key] = global.fetch('/workflows/models', {
      method: 'POST',
      headers: { 'X-CSRF-Token': global.CSRF_TOKEN },
      body: new URLSearchParams({ provider: ref }),
    }).then(function (response) {
      if (!response.ok) throw new Error('model catalog request failed');
      return response.json();
    }).then(function (data) {
      var models = normalizedModels(data && data.models);
      memory[key] = models;
      writeSession(key, models);
      return models.slice();
    }).finally(function () {
      delete pending[key];
    });
    return pending[key];
  }

  global.LocModelCatalog = { get: get };
})(window);
