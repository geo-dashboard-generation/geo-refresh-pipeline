/**
 * Stale-data badge for a geo-refresh-pipeline manifest.
 *
 * Dependency-free, ~4 kB, no build step. It fetches `manifest.json`, works out
 * how old the data is and renders a small badge: "Updated 3 hours ago", or a
 * warning when a source has passed its configured `max_age`.
 *
 * The age is recomputed in the browser from `fetched_at`, not read from the
 * manifest's `stale` flag alone, so a page left open overnight goes stale on
 * its own without a reload.
 *
 * Usage:
 *
 *   <div id="data-freshness"></div>
 *   <script src="freshness-badge.js"></script>
 *   <script>
 *     geoRefreshBadge.mount({ el: '#data-freshness', manifest: '/manifest.json' });
 *   </script>
 *
 * @module freshness-badge
 */
(function (global) {
  'use strict';

  /** @typedef {{state: 'fresh'|'stale'|'failed'|'unknown', label: string, title: string, ageSeconds: number|null, sources: Array<Object>}} BadgeModel */

  var UNITS = [
    [31536000, 'year'],
    [2592000, 'month'],
    [604800, 'week'],
    [86400, 'day'],
    [3600, 'hour'],
    [60, 'minute']
  ];

  /**
   * Render an age in seconds as "3 hours ago". Mirrors the Python
   * `humanize_age` so the CLI and the page never disagree.
   *
   * @param {number} seconds Age in seconds.
   * @returns {string} Human-readable age.
   */
  function humanizeAge(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return 'unknown';
    if (seconds < 0) return 'in the future';
    if (seconds < 45) return 'just now';
    for (var i = 0; i < UNITS.length; i++) {
      var size = UNITS[i][0];
      var name = UNITS[i][1];
      if (seconds >= size) {
        var count = Math.floor(seconds / size);
        return count + ' ' + name + (count === 1 ? '' : 's') + ' ago';
      }
    }
    return Math.floor(seconds) + ' seconds ago';
  }

  /**
   * Parse an ISO 8601 timestamp into epoch milliseconds.
   *
   * @param {string|null|undefined} text Timestamp, e.g. "2026-07-19T08:30:00Z".
   * @returns {number|null} Epoch milliseconds, or null when unparseable.
   */
  function parseTimestamp(text) {
    if (!text) return null;
    var value = Date.parse(text);
    return isNaN(value) ? null : value;
  }

  /**
   * Reduce a manifest document to everything the badge needs to draw itself.
   *
   * @param {Object} manifest Parsed manifest.json.
   * @param {Object} [options] Options.
   * @param {string} [options.source] Report only this source instead of the
   *   oldest one across the whole manifest.
   * @param {number} [options.now] Reference time in epoch ms (for tests).
   * @returns {BadgeModel} The badge model.
   */
  function evaluate(manifest, options) {
    options = options || {};
    var now = options.now || Date.now();
    var entries = [];
    var all = (manifest && manifest.sources) || {};
    Object.keys(all).forEach(function (name) {
      if (options.source && name !== options.source) return;
      var entry = all[name];
      var fetchedAt = parseTimestamp(entry.fetched_at);
      var age = fetchedAt === null ? null : Math.max(0, (now - fetchedAt) / 1000);
      var maxAge = typeof entry.max_age_seconds === 'number' ? entry.max_age_seconds : null;
      entries.push({
        name: name,
        ageSeconds: age,
        maxAgeSeconds: maxAge,
        featureCount: entry.feature_count || 0,
        failed: entry.status === 'failed',
        stale: maxAge !== null && (age === null || age > maxAge),
        error: entry.error || null
      });
    });

    if (!entries.length) {
      return {
        state: 'unknown',
        label: 'No data',
        title: 'The manifest lists no matching sources.',
        ageSeconds: null,
        sources: entries
      };
    }

    var oldest = entries[0];
    entries.forEach(function (entry) {
      if (entry.ageSeconds === null) oldest = entry;
      else if (oldest.ageSeconds !== null && entry.ageSeconds > oldest.ageSeconds) oldest = entry;
    });

    var failed = entries.filter(function (e) { return e.failed; });
    var stale = entries.filter(function (e) { return e.stale; });
    var total = entries.reduce(function (sum, e) { return sum + e.featureCount; }, 0);
    var ageText = humanizeAge(oldest.ageSeconds);

    if (failed.length) {
      return {
        state: 'failed',
        label: 'Refresh failed — showing data from ' + ageText,
        title: failed.map(function (e) { return e.name + ': ' + (e.error || 'failed'); }).join('\n'),
        ageSeconds: oldest.ageSeconds,
        sources: entries
      };
    }
    if (stale.length) {
      return {
        state: 'stale',
        label: 'Data may be out of date — updated ' + ageText,
        title: stale.map(function (e) {
          return e.name + ' is older than its ' + humanizeAge(e.maxAgeSeconds).replace(' ago', '') + ' limit';
        }).join('\n'),
        ageSeconds: oldest.ageSeconds,
        sources: entries
      };
    }
    return {
      state: 'fresh',
      label: 'Updated ' + ageText,
      title: total + ' features across ' + entries.length + ' source(s)',
      ageSeconds: oldest.ageSeconds,
      sources: entries
    };
  }

  var ICONS = {
    fresh: '●',
    stale: '▲',
    failed: '■',
    unknown: '○'
  };

  /**
   * Draw a badge model into an element.
   *
   * @param {Element} element Target element.
   * @param {BadgeModel} model Model from `evaluate`.
   * @returns {void}
   */
  function paint(element, model) {
    element.className = 'geo-refresh-badge geo-refresh-badge--' + model.state;
    element.setAttribute('role', 'status');
    element.setAttribute('title', model.title);
    element.setAttribute('data-state', model.state);
    element.textContent = '';

    var icon = document.createElement('span');
    icon.className = 'geo-refresh-badge__icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = ICONS[model.state] || ICONS.unknown;

    var label = document.createElement('span');
    label.className = 'geo-refresh-badge__label';
    label.textContent = model.label;

    element.appendChild(icon);
    element.appendChild(label);
  }

  /**
   * Fetch a manifest, render the badge and keep the relative time current.
   *
   * @param {Object} options Options.
   * @param {string|Element} options.el Target element or CSS selector.
   * @param {string} [options.manifest='manifest.json'] Manifest URL.
   * @param {string} [options.source] Restrict the badge to one source.
   * @param {number} [options.refreshMs=60000] How often to redraw the relative
   *   time. Pass 0 to draw once.
   * @param {number} [options.reloadMs=0] How often to re-fetch the manifest.
   *   0 disables re-fetching.
   * @param {function(BadgeModel):void} [options.onUpdate] Called after each draw.
   * @returns {{stop: function():void, refresh: function():Promise<BadgeModel>}}
   *   Handle for stopping the timers or forcing a re-fetch.
   */
  function mount(options) {
    options = options || {};
    var element = typeof options.el === 'string' ? document.querySelector(options.el) : options.el;
    if (!element) throw new Error('geoRefreshBadge.mount: element ' + options.el + ' not found');
    var url = options.manifest || 'manifest.json';
    var refreshMs = options.refreshMs === undefined ? 60000 : options.refreshMs;
    var reloadMs = options.reloadMs || 0;
    var document_ = null;
    var timers = [];

    function draw() {
      var model = document_
        ? evaluate(document_, { source: options.source })
        : { state: 'unknown', label: 'Loading…', title: '', ageSeconds: null, sources: [] };
      paint(element, model);
      if (options.onUpdate) options.onUpdate(model);
      return model;
    }

    function refresh() {
      return fetch(url, { cache: 'no-cache' })
        .then(function (response) {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return response.json();
        })
        .then(function (payload) {
          document_ = payload;
          return draw();
        })
        .catch(function (error) {
          paint(element, {
            state: 'failed',
            label: 'Freshness unknown',
            title: 'Could not load ' + url + ': ' + error.message,
            ageSeconds: null,
            sources: []
          });
          return null;
        });
    }

    draw();
    refresh();
    if (refreshMs > 0) timers.push(setInterval(draw, refreshMs));
    if (reloadMs > 0) timers.push(setInterval(refresh, reloadMs));

    return {
      stop: function () {
        timers.forEach(clearInterval);
        timers = [];
      },
      refresh: refresh
    };
  }

  global.geoRefreshBadge = {
    mount: mount,
    evaluate: evaluate,
    humanizeAge: humanizeAge,
    parseTimestamp: parseTimestamp,
    paint: paint
  };
})(typeof window !== 'undefined' ? window : globalThis);
