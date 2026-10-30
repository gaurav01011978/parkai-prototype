/* ParkAI - "Faster Parking Option" suggestion (User/Driver page).
 *
 * user.html me sirf 2 chhote badlav chahiye:
 *   1) main script se pehle is file ka script tag (src="/static/alt.js")
 *   2) Select button ke click par:  window.parkaiSelect(lot)
 *
 * Yeh file khud apna CSS (static/alt.css), "Starting point" dropdown, popup aur toast banati hai.
 * Koi bhi problem ho to normal selection hi chalta hai.
 */
(function () {
  'use strict';

  var API_URL = '/api/suggest-alternative';
  var REQUEST_TIMEOUT_MS = 12000;
  var GPS_TIMEOUT_MS = 8000;

  // Demo starting points (Delhi - lots ke aas-paas). Real use me "My current location (GPS)" chuno.
  var START_POINTS = [
    ['28.5285,77.2193', 'Saket (demo start)'],
    ['28.5672,77.2100', 'AIIMS (demo start)'],
    ['28.6129,77.2295', 'India Gate (demo start)'],
    ['28.6315,77.2167', 'Connaught Place (demo start)'],
    ['gps', 'My current location (GPS)']
  ];

  var checkId = 0;          // sirf sabse naye click ka jawab use hota hai
  var toastTimer = null;
  var modal = null;         // abhi khula popup (ya null)

  // ---------- chhote helpers ----------
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;   // textContent = lot ke naam se XSS nahi
    return node;
  }

  function fmtDist(m) {
    if (m === null || m === undefined) return '-';
    return m >= 1000 ? (m / 1000).toFixed(1) + ' km' : Math.round(m) + ' m';
  }

  function loadStyles() {
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/static/alt.css?v=1';
    document.head.appendChild(link);
  }

  // ---------- "Starting point" dropdown ----------
  function buildStartPicker() {
    var anchor = document.querySelector('.fields');
    if (!anchor || document.getElementById('origin')) return;
    var box = el('div', 'alt-start');
    var label = el('label', null, 'Starting point');
    label.htmlFor = 'origin';
    var select = el('select');
    select.id = 'origin';
    START_POINTS.forEach(function (p) {
      var opt = el('option', null, p[1]);
      opt.value = p[0];
      select.appendChild(opt);
    });
    box.appendChild(label);
    box.appendChild(select);
    anchor.insertAdjacentElement('afterend', box);
  }

  function getOrigin() {
    var value = document.getElementById('origin').value;
    if (value !== 'gps') {
      var parts = value.split(',').map(Number);
      return Promise.resolve({ lat: parts[0], lng: parts[1] });
    }
    return new Promise(function (resolve, reject) {
      if (!navigator.geolocation) { reject(new Error('gps-unsupported')); return; }
      navigator.geolocation.getCurrentPosition(
        function (p) { resolve({ lat: p.coords.latitude, lng: p.coords.longitude }); },
        function () { reject(new Error('gps-denied')); },
        { timeout: GPS_TIMEOUT_MS, maximumAge: 120000 }
      );
    });
  }

  function currentTrip() {
    var parts = document.getElementById('dest').value.split(',');
    var hour = document.getElementById('hourSelect').value;
    return {
      lat: Number(parts[0]), lng: Number(parts[1]), name: parts[2],
      hour: hour ? Number(hour) : null,
      key: document.getElementById('dest').value + '|' + hour + '|' + document.getElementById('origin').value
    };
  }

  // ---------- toast (loading / error / no-alternative) ----------
  function hideToast() {
    var t = document.getElementById('alt-toast');
    if (t) t.classList.remove('alt-show');
  }

  function toast(kind, text, autoHideMs) {
    var t = document.getElementById('alt-toast');
    if (!t) {
      t = el('div', 'alt-toast');
      t.id = 'alt-toast';
      t.setAttribute('role', 'status');
      t.setAttribute('aria-live', 'polite');
      document.body.appendChild(t);
    }
    t.className = 'alt-toast alt-toast-' + kind + ' alt-show';
    t.textContent = '';
    if (kind === 'loading') t.appendChild(el('span', 'alt-spinner'));
    t.appendChild(el('span', null, text));
    clearTimeout(toastTimer);
    if (autoHideMs) toastTimer = setTimeout(hideToast, autoHideMs);
  }

  // ---------- popup ----------
  function trafficInfo(lot) {
    var d = lot.delay_minutes;
    var delay = d === null || d === undefined ? '' : (d < 1 ? '<1 min' : d + ' min');
    if (lot.traffic_level === 'clear') return { text: 'Light traffic', warn: false };
    if (lot.traffic_level === 'moderate') return { text: 'Moderate · +' + delay + ' delay', warn: true };
    if (lot.traffic_level === 'heavy') return { text: 'Heavy · +' + delay + ' delay', warn: true };
    return { text: 'Not available', warn: false };
  }

  function buildColumn(kind, lot, savedMinutes) {
    var col = el('div', 'alt-col alt-col-' + kind);
    col.appendChild(el('div', 'alt-tag', kind === 'alt' ? 'Faster option' : 'Your selection'));
    col.appendChild(el('div', 'alt-lot-name', lot.name));
    if (kind === 'alt') col.appendChild(el('div', 'alt-saves', 'Saves ' + savedMinutes + ' min'));

    var traffic = trafficInfo(lot);
    var rows = [
      ['Driving time (live traffic)', lot.traffic_minutes + ' min', 'time'],
      ['Road distance', fmtDist(lot.road_distance_m), ''],
      ['Traffic', traffic.text, traffic.warn ? 'warn' : ''],
      ['Free spaces', lot.free + ' free', ''],
      ['From destination', fmtDist(lot.distance_to_destination_m), '']
    ];
    rows.forEach(function (r) {
      var row = el('div', 'alt-row');
      row.appendChild(el('span', 'alt-row-label', r[0]));
      row.appendChild(el('strong', 'alt-row-value' + (r[2] ? ' alt-' + r[2] : ''), r[1]));
      col.appendChild(row);
    });
    return col;
  }

  function closeModal() {
    if (!modal) return;
    document.removeEventListener('keydown', modal.onKey, true);
    document.body.classList.remove('alt-lock');
    modal.root.remove();
    var back = modal.returnFocus;
    modal = null;
    if (back && back.focus) back.focus();
  }

  function openModal(lot, result, trip) {
    closeModal();
    var sel = result.selected, alt = result.alternative;
    var walk = result.extra_walk_m;

    var root = el('div', 'alt-overlay');
    var dialog = el('div', 'alt-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'alt-title');

    var title = el('h3', 'alt-title', 'We Found a Faster Parking Option!');
    title.id = 'alt-title';
    var walkText = walk > 0 ? walk + ' m farther from ' + trip.name + ', but'
      : walk < 0 ? Math.abs(walk) + ' m closer to ' + trip.name + ' and'
        : 'the same distance from ' + trip.name + ' and';
    var sub = el('p', 'alt-sub', alt.name + ' is ' + walkText + ' could save you about ' +
      result.saved_minutes + ' min of driving.');

    var cols = el('div', 'alt-cols');
    cols.appendChild(buildColumn('orig', sel, 0));
    cols.appendChild(buildColumn('alt', alt, result.saved_minutes));

    var actions = el('div', 'alt-actions');
    var keep = el('button', 'alt-btn alt-btn-secondary', 'Continue with Selected Lot');
    var swap = el('button', 'alt-btn alt-btn-primary', 'Switch to Alternative');
    keep.type = 'button';
    swap.type = 'button';
    actions.appendChild(keep);
    actions.appendChild(swap);

    dialog.appendChild(title);
    dialog.appendChild(sub);
    dialog.appendChild(cols);
    dialog.appendChild(actions);
    dialog.appendChild(el('p', 'alt-note', 'Nothing is reserved. Live traffic from Google Maps; times are estimates.'));
    root.appendChild(dialog);

    // Popup band karne ke saare tareeke = "Continue with Selected Lot" (selection kabhi apne-aap nahi badalta)
    function keepOriginal() {
      closeModal();
      window.selectLot(lot.name, lot.free, lot.distance_m);
    }
    function switchToAlt() {
      closeModal();
      window.selectLot(alt.name, alt.free, alt.distance_to_destination_m);
    }
    keep.addEventListener('click', keepOriginal);
    swap.addEventListener('click', switchToAlt);
    root.addEventListener('mousedown', function (e) { if (e.target === root) keepOriginal(); });

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); keepOriginal(); return; }
      if (e.key === 'Tab') {                      // Tab sirf popup ke buttons ke beech ghume
        var buttons = dialog.querySelectorAll('button');
        var first = buttons[0], last = buttons[buttons.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    modal = { root: root, onKey: onKey, returnFocus: document.activeElement };
    document.addEventListener('keydown', onKey, true);
    document.body.classList.add('alt-lock');
    document.body.appendChild(root);
    swap.focus({ preventScroll: true });
  }

  // ---------- main entry: Select button ----------
  function plainSelect(lot) {
    window.selectLot(lot.name, lot.free, lot.distance_m);
  }

  async function parkaiSelect(lot) {
    var myCheck = ++checkId;
    var trip;
    try {
      trip = currentTrip();
    } catch (e) {
      plainSelect(lot);            // page structure alag nikla -> normal selection
      return;
    }
    toast('loading', 'Checking live traffic…');

    var result = null, problem = null;
    var timer = null;
    try {
      var origin = await getOrigin();
      var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      if (ctrl) timer = setTimeout(function () { ctrl.abort(); }, REQUEST_TIMEOUT_MS);
      var res = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lot_id: lot.id, origin: origin,
          destination: { lat: trip.lat, lng: trip.lng }, hour: trip.hour
        }),
        signal: ctrl ? ctrl.signal : undefined
      });
      if (!res.ok) throw new Error('http-' + res.status);
      result = await res.json();
    } catch (e) {
      problem = (e && (e.message === 'gps-denied' || e.message === 'gps-unsupported')) ? 'gps' : 'error';
    } finally {
      if (timer) clearTimeout(timer);
    }

    if (myCheck !== checkId) return;               // beech me naya click aa gaya -> yeh jawab purana hai
    var changedMeanwhile = currentTripKeySafe() !== trip.key;

    if (!problem && !changedMeanwhile && result && result.status === 'found') {
      hideToast();
      openModal(lot, result, trip);                // yahan selection abhi final nahi hota
      return;
    }

    plainSelect(lot);                              // baaki sab cases me normal selection
    if (changedMeanwhile) { hideToast(); return; }
    if (problem === 'gps') {
      toast('warn', "Couldn't read your location, so traffic wasn't checked. Lot selected as usual.", 5000);
    } else if (problem || !result || result.status === 'unavailable') {
      toast('warn', 'Live traffic is unavailable right now. Lot selected as usual.', 5000);
    } else {
      toast('ok', 'No faster parking option found — your selection is already the quickest suitable one.', 4500);
    }
  }

  function currentTripKeySafe() {
    try { return currentTrip().key; } catch (e) { return null; }
  }

  loadStyles();
  buildStartPicker();
  window.parkaiSelect = parkaiSelect;
})();