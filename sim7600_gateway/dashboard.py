"""Single-page local dashboard (no build step, no external assets)."""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Athena GSM · SIM7600</title>
<style>
:root { --bg:#0b0f14; --card:#121a24; --line:#1f2c3a; --text:#e8eef6; --muted:#8aa0b6;
        --accent:#59d2a4; --accent2:#5aa9ff; --danger:#ff6b6b; --warn:#ffc857; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif; background:
       radial-gradient(1200px 600px at 20% -10%, #16324a55, transparent), var(--bg); color:var(--text); }
header { display:flex; align-items:center; justify-content:space-between; gap:16px;
         padding:18px 26px; border-bottom:1px solid var(--line); position:sticky; top:0;
         backdrop-filter:blur(8px); background:#0b0f14cc; z-index:5; flex-wrap:wrap; }
h1 { font-size:18px; margin:0; letter-spacing:.2px; }
h1 span { color:var(--accent); }
main { padding:22px; max-width:1280px; margin:0 auto; display:grid; gap:18px;
       grid-template-columns: minmax(0,1.55fr) minmax(300px,.9fr); }
@media (max-width:980px) { main { grid-template-columns:1fr; } }
.card { background:linear-gradient(180deg,#131c27,#101720); border:1px solid var(--line);
        border-radius:16px; padding:18px; }
.card h2 { margin:0 0 12px; font-size:13px; text-transform:uppercase; letter-spacing:.12em; color:var(--muted); }
.badge { display:inline-flex; align-items:center; gap:7px; border:1px solid var(--line);
         padding:6px 11px; border-radius:999px; font-size:12px; color:var(--muted); }
.dot { width:8px; height:8px; border-radius:50%; background:var(--danger); }
.dot.on { background:var(--accent); box-shadow:0 0 10px var(--accent); }
video, img.stream { width:100%; border-radius:12px; background:#000; display:block; min-height:200px; object-fit:cover; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; }
.kv { background:#0d141d; border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
.kv b { display:block; font-size:17px; font-weight:600; margin-top:3px; }
.kv small { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
.row { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
input, select { flex:1 1 120px; min-width:0; background:#0a1118; color:var(--text);
                border:1px solid var(--line); border-radius:9px; padding:10px 12px; font:inherit; }
button { background:#1b2a3a; color:var(--text); border:1px solid #2a3d52; border-radius:9px;
         padding:10px 14px; font:inherit; cursor:pointer; }
button:hover { border-color:var(--accent); }
button.primary { background:var(--accent); color:#062015; border-color:var(--accent); font-weight:600; }
button.danger { border-color:#5a2a2a; color:var(--danger); }
#log { height:300px; overflow:auto; font:12px/1.5 ui-monospace,Consolas,monospace; color:#b9cadb; }
#log div { padding:3px 0; border-bottom:1px dashed #1b2836; }
#log .t { color:var(--accent2); }
#log .w { color:var(--warn); }
.muted { color:var(--muted); }
.toast { position:fixed; right:18px; bottom:18px; background:#152131; border:1px solid #2a3d52;
         padding:12px 16px; border-radius:12px; opacity:0; transform:translateY(10px);
         transition:.25s; pointer-events:none; }
.toast.show { opacity:1; transform:none; }
</style>
</head>
<body>
<header>
  <h1>Athena <span>GSM</span> · SIM7600</h1>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span class="badge"><span id="athenaDot" class="dot"></span><span id="athenaText">Athena</span></span>
    <span class="badge"><span id="modemDot" class="dot"></span><span id="modemText">Модем</span></span>
    <span class="badge"><span id="camDot" class="dot"></span><span id="camText">Камера</span></span>
    <button id="tokenBtn" class="muted">Токен API</button>
  </div>
</header>
<main>
  <section style="display:grid;gap:18px">
    <div class="card">
      <h2>Камера</h2>
      <img class="stream" id="stream" alt="Камера">
      <div class="row">
        <button class="primary" onclick="act('/api/camera/snapshot.jpg?save=true')">Снимок</button>
        <button onclick="act('/api/camera/record',{seconds:12})">Записать 12 c</button>
        <button onclick="act('/api/modem/reset')">Сброс модема</button>
      </div>
    </div>
    <div class="card">
      <h2>Модем</h2>
      <div class="grid" id="tiles"></div>
      <div class="row">
        <input id="number" placeholder="+7 999 123-45-67" inputmode="tel">
        <button onclick="dial()">Позвонить</button>
        <button onclick="act('/api/calls/answer')">Ответить</button>
        <button class="danger" onclick="act('/api/calls/hangup')">Сбросить</button>
      </div>
      <div class="row">
        <input id="smsText" placeholder="Текст SMS">
        <button onclick="sms()">Отправить SMS</button>
        <input id="ussd" placeholder="*100#" style="max-width:130px">
        <button onclick="ussd()">USSD</button>
      </div>
    </div>
  </section>
  <section style="display:grid;gap:18px">
    <div class="card">
      <h2>События</h2>
      <div id="log"></div>
    </div>
    <div class="card">
      <h2>Последние SMS</h2>
      <div id="smsList" class="muted" style="font-size:13px">—</div>
    </div>
  </section>
</main>
<div class="toast" id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
const BASE = window.location.pathname.replace(/\/+$/, '');
let token = localStorage.getItem('gwToken') || '';
function headers() { return token ? {'Authorization':'Bearer '+token} : {}; }
function toast(msg, bad) {
  const el = $('toast'); el.textContent = msg; el.style.borderColor = bad ? '#5a2a2a' : '#2a3d52';
  el.classList.add('show'); clearTimeout(el._t); el._t = setTimeout(()=>el.classList.remove('show'), 3200);
}
async function api(path, body) {
  path = BASE + path;
  const options = {method: body === undefined ? 'GET' : 'POST', headers: {...headers()}};
  if (body !== undefined) { options.headers['Content-Type']='application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null; try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!response.ok) throw new Error((data && data.detail) || response.statusText);
  return data;
}
async function act(path, body) {
  try { await api(path, body); toast('Готово'); setTimeout(refresh, 500); }
  catch (e) { toast(e.message || 'Ошибка', true); }
}
async function dial() {
  const number = $('number').value.trim(); if (!number) return toast('Введите номер', true);
  await act('/api/dial', {number});
}
async function sms() {
  const number = $('number').value.trim(), text = $('smsText').value.trim();
  if (!number || !text) return toast('Нужен номер и текст', true);
  await act('/api/sms', {number, text});
}
async function ussd() {
  const code = $('ussd').value.trim(); if (!code) return;
  try { const r = await api('/api/ussd', {code}); toast(r.response || 'USSD OK'); }
  catch (e) { toast(e.message || 'Ошибка USSD', true); }
}
$('tokenBtn').onclick = () => {
  const value = prompt('API token (config bridge.api_token):', token);
  if (value !== null) { token = value.trim(); localStorage.setItem('gwToken', token); refresh(); connect(); }
};
function tile(label, value, unit='') {
  return `<div class="kv"><small>${label}</small><b>${value ?? '—'}${unit}</b></div>`;
}
async function refresh() {
  try {
    const s = await api('/api/status');
    const m = s.modem || {}, c = s.camera || {}, a = s.athena || {};
    $('tiles').innerHTML = [
      tile('Состояние', m.state), tile('Оператор', m.operator || '—'),
      tile('Сигнал', m.signal_percent ?? '—', '%'), tile('Сеть', m.network || '—'),
      tile('Регистрация', m.registered ? 'да' : 'нет'), tile('SIM', m.sim_state || '—'),
      tile('Звонок', m.call_active ? 'активен' : 'нет'), tile('SMS', (m.sms_sent||0)+' / '+(m.sms_received||0)),
    ].join('');
    $('athenaDot').className = 'dot ' + (a.connected ? 'on' : '');
    $('athenaText').textContent = a.connected ? 'Athena: онлайн' : 'Athena: нет связи';
    $('modemDot').className = 'dot ' + (m.registered ? 'on' : '');
    $('modemText').textContent = m.registered ? 'Модем: регистрация' : 'Модем: ' + (m.state || '—');
    $('camDot').className = 'dot ' + (c.available ? 'on' : '');
    $('camText').textContent = c.available ? `Камера: ${c.width}×${c.height}` : 'Камера: нет кадра';
    const sms = await api('/api/sms/recent?limit=6').catch(()=>[]);
    $('smsList').innerHTML = sms.length ? sms.map(x =>
      `<div style="padding:7px 0;border-bottom:1px dashed #1b2836"><b>${x.number || '—'}</b>
       <span class="muted">${x.direction === 'out' ? '→' : '←'} ${x.at || ''}</span><br>${(x.text||'').slice(0,110)}</div>`
    ).join('') : '<span class="muted">Пока нет сообщений</span>';
  } catch (e) { /* keep UI quiet */ }
}
$('stream').src = BASE + '/api/camera/stream.mjpg' + (token ? '?token='+encodeURIComponent(token) : '');
let source;
function connect() {
  if (source) source.close();
  const url = BASE + '/api/events' + (token ? '?token='+encodeURIComponent(token) : '');
  source = new EventSource(url);
  source.onmessage = (e) => {
    try { const r = JSON.parse(e.data); addLog(r.type, r.data); } catch (err) {}
  };
  source.onerror = () => {};
}
function addLog(type, data) {
  const line = document.createElement('div');
  const time = new Date().toLocaleTimeString();
  const relevant = ['call.incoming','call.state','sms.incoming','sms.sent','dtmf','device.event',
                    'camera.motion','camera.snapshot','command.received','modem.error','intercom.active','intercom.ended'];
  if (!relevant.includes(type)) return;
  let detail = '';
  if (type === 'call.incoming') detail = 'звонок от ' + data.number;
  else if (type === 'call.state') detail = `${data.state} ${data.number || ''} ${data.duration_sec || ''}`;
  else if (type === 'sms.incoming') detail = `SMS от ${data.number}: ${(data.text||'').slice(0,80)}`;
  else if (type === 'sms.sent') detail = `SMS отправлена ${data.number}`;
  else if (type === 'dtmf') detail = 'DTMF ' + data.digit;
  else if (type === 'device.event') detail = data.event + ': ' + JSON.stringify(data.data || {}).slice(0,100);
  else if (type === 'camera.snapshot') detail = 'снимок ' + (data.filename || '');
  else if (type === 'camera.motion') detail = 'движение ' + Math.round((data.changed||0)*100) + '%';
  else detail = JSON.stringify(data).slice(0,120);
  line.innerHTML = `<span class="t">${time}</span> <b>${type}</b> — ${detail}`;
  const log = $('log'); log.prepend(line);
  while (log.childNodes.length > 120) log.removeChild(log.lastChild);
}
refresh(); connect();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
