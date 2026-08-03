let es = null;
let adminPrefix = '';
let isHistorical = false;
const MAX_ROWS = 10;

function initLogs(prefix) {
  adminPrefix = prefix;
  connect();
}

function connect(filters) {
  if (es) { es.close(); }

  const today = new Date().toISOString().slice(0, 10);
  isHistorical = !!(filters?.date && filters.date !== today);

  const params = new URLSearchParams();
  if (filters?.caller)   params.set('caller_id', filters.caller);
  if (filters?.result)   params.set('result',    filters.result);
  if (filters?.uniqueid) params.set('uniqueid',  filters.uniqueid);
  if (filters?.session)  params.set('session',   filters.session);
  if (filters?.date)     params.set('date',      filters.date);

  const url = `${adminPrefix}/logs/stream` + (params.toString() ? '?' + params : '');

  setStatus('connecting');
  es = new EventSource(url);

  // Lote inicial — servidor envía ASC (oldest first), prependRow pone newest arriba
  es.addEventListener('init', e => {
    clearTable();
    const rows = JSON.parse(e.data);
    rows.forEach(row => prependRow(row));
    updateCount();
    if (isHistorical) {
      // Un día pasado no recibe filas nuevas — el servidor ya cerró el
      // stream después de este lote. Cerramos del lado del cliente para que
      // EventSource no intente reconectar solo cada 5s a lo mismo.
      es.close();
      setStatus('historical');
    } else {
      setStatus('live');
    }
  });

  // Nuevas filas en tiempo real
  es.onmessage = e => {
    prependRow(JSON.parse(e.data));
    trimTable();
    updateCount();
  };

  es.onerror = () => {
    if (isHistorical) return;   // cierre esperado, no es una desconexión real
    setStatus('disconnected');
    // Reconectar después de 5 segundos
    setTimeout(() => connect(currentFilters()), 5000);
  };
}

function clearTable() {
  const tbody = document.getElementById('log-body');
  tbody.innerHTML = '';
}

function appendRow(row) {
  const tbody = document.getElementById('log-body');
  const tr = buildRow(row);
  tbody.appendChild(tr);
}

function prependRow(row) {
  // Quitar fila "esperando" si existe
  const empty = document.getElementById('empty-row');
  if (empty) empty.remove();

  const tbody = document.getElementById('log-body');
  const tr = buildRow(row);
  tbody.insertBefore(tr, tbody.firstChild);
}

function fmtDatetime(dt) {
  if (!dt) return '';
  // dt viene como "2026-06-28 22:59:14" desde Python
  const d = new Date(dt.replace(' ', 'T'));
  if (isNaN(d)) return dt;
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toTimeString().slice(0, 8);
  if (sameDay) return time;
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}/${mm} ${time}`;
}

function buildRow(r) {
  const tr = document.createElement('tr');
  const resultClass = `result-${r.result || 'UNKNOWN'}`;
  const transcript  = r.transcript || '';
  const sessionId   = r.param2 || '';
  const leadId      = r.param1 || '';
  const p4parts     = (r.param4 || '').split('|');
  const campaignId  = p4parts[0] || '';
  const listId      = p4parts[1] || '';
  const layerIcon = r.layer === 2
    ? '2 <span title="Transcripción" style="color:#3b82f6">🧠</span>'
    : '1 <span title="Energía" style="color:#94a3b8">⚡</span>';
  const modeHtml = r.mode === 'stream'
    ? '<span style="color:#06b6d4;font-size:.75rem">STREAM</span>'
    : '<span style="color:#94a3b8;font-size:.75rem">BATCH</span>';
  const _pmap = {
    'deepgramv2': ['#10b981', 'DG v2 ⚡'],
    'deepgram':   ['#a855f7', 'DEEPGRAM'],
    'fireworks':  ['#f59e0b', 'FIREWORKS'],
    'groq':       ['#3b82f6', 'GROQ'],
    'together':   ['#06b6d4', 'TOGETHER'],
    'openai':     ['#10a37f', 'OPENAI'],
    'vosk':       ['#8b5cf6', 'VOSK'],
    'vosk_stream':['#8b5cf6', 'VOSK ⚡'],
    'sherpa':     ['#8b5cf6', 'SHERPA'],
  };
  const _pm = r.provider && _pmap[r.provider];
  const providerHtml = _pm
    ? `<span style="color:${_pm[0]};font-weight:600;font-size:.75rem">${_pm[1]}</span>`
    : '<span style="color:#475569">—</span>';
  const dim = 'font-size:.75rem; color:#64748b;';

  let transcriptHtml;
  if (!transcript || transcript === '') {
    transcriptHtml = '<span style="color:#475569">—</span>';
  } else if (transcript === '[sin audio]') {
    transcriptHtml = '<span style="color:#475569; font-size:.8rem">sin audio</span>';
  } else if (transcript === '[silencio]') {
    transcriptHtml = '<span style="color:#64748b; font-size:.8rem">silencio</span>';
  } else {
    transcriptHtml = `<span style="font-style:italic; color:#e2e8f0">"${esc(transcript)}"</span>`;
  }

  tr.innerHTML = `
    <td style="white-space:nowrap">${fmtDatetime(r.datetime || r.time)}</td>
    <td>${esc(r.client || '')}</td>
    <td>${esc(r.caller_id || '')}</td>
    <td style="${dim}">${esc(leadId)}</td>
    <td style="${dim}">${esc(campaignId)}</td>
    <td style="${dim}">${esc(listId)}</td>
    <td class="${resultClass}">${r.result || ''}</td>
    <td>${layerIcon}</td>
    <td>${modeHtml}</td>
    <td>${providerHtml}</td>
    <td title="Detección experimental de tono — no participa en el resultado">${beepDetected(r.beep_detected)}</td>
    <td>${r.latency_ms != null ? r.latency_ms : ''}</td>
    <td>${transcriptHtml}</td>
    <td style="font-size:.7rem; color:#475569; font-family:monospace; cursor:${sessionId ? 'pointer' : 'default'}"
        title="${sessionId ? 'Clic para filtrar por esta sesión' : ''}"
        onclick="${sessionId ? `filterBySession('${esc(sessionId)}')` : ''}">${esc(sessionId)}</td>
  `;
  return tr;
}

function beepDetected(val) {
  const on = val === 1 || val === '1' || val === true;
  return on
    ? '<span style="color:#f59e0b" title="Tono detectado">🔔</span>'
    : '<span style="color:#334155">—</span>';
}

function trimTable() {
  const tbody = document.getElementById('log-body');
  while (tbody.rows.length > MAX_ROWS) {
    tbody.deleteRow(tbody.rows.length - 1);
  }
}

function updateCount() {
  const n = document.getElementById('log-body').rows.length;
  document.getElementById('row-count').textContent = n;
}

function setStatus(state) {
  const el = document.getElementById('status-indicator');
  el.className = 'status-dot status-' + (state === 'historical' ? 'live' : state);
  el.textContent = state === 'live'       ? '● En vivo' :
                    state === 'historical' ? '● Vista histórica (día seleccionado)' :
                    state === 'connecting' ? 'Conectando...' : '● Desconectado';
}

function currentFilters() {
  return {
    caller:   document.getElementById('f-caller')?.value.trim(),
    result:   document.getElementById('f-result')?.value,
    uniqueid: document.getElementById('f-uniqueid')?.value.trim(),
    session:  document.getElementById('f-session')?.value.trim(),
    date:     document.getElementById('f-date')?.value,
  };
}

function applyFilters() {
  connect(currentFilters());
}

function clearFilters() {
  document.getElementById('f-caller').value   = '';
  document.getElementById('f-result').value   = '';
  document.getElementById('f-uniqueid').value = '';
  document.getElementById('f-session').value  = '';
  document.getElementById('f-date').value     = '';
  connect();
}

function filterBySession(sid) {
  const el = document.getElementById('f-session');
  if (el) { el.value = sid; applyFilters(); }
}

function esc(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
