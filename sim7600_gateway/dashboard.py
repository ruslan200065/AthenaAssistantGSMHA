"""Local dashboard: modem status, calls, SMS/USSD, live events."""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Athena GSM · SIM7600</title>
<style>
:root{--bg:#0b0f14;--card:#121a24;--line:#1f2c3a;--text:#e8eef6;--muted:#8aa0b6;--accent:#59d2a4;--accent2:#5aa9ff;--danger:#ff6b6b}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:radial-gradient(1200px 600px at 20% -10%,#16324a55,transparent),var(--bg);color:var(--text)}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 26px;border-bottom:1px solid var(--line);position:sticky;top:0;backdrop-filter:blur(8px);background:#0b0f14cc;z-index:5;flex-wrap:wrap}
h1{font-size:18px;margin:0}h1 span{color:var(--accent)}main{padding:22px;max-width:1100px;margin:0 auto;display:grid;gap:18px;grid-template-columns:minmax(0,1.5fr) minmax(300px,.9fr)}
@media(max-width:980px){main{grid-template-columns:1fr}}
.card{background:linear-gradient(180deg,#131c27,#101720);border:1px solid var(--line);border-radius:16px;padding:18px}
.card h2{margin:0 0 12px;font-size:13px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}
.badge{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);padding:6px 11px;border-radius:999px;font-size:12px;color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--danger)}.dot.on{background:var(--accent);box-shadow:0 0 10px var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.kv{background:#0d141d;border:1px solid var(--line);border-radius:10px;padding:10px 12px}.kv b{display:block;font-size:17px;margin-top:3px}.kv small{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}input{flex:1 1 120px;min-width:0;background:#0a1118;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px 12px;font:inherit}
button{background:#1b2a3a;color:var(--text);border:1px solid #2a3d52;border-radius:9px;padding:10px 14px;font:inherit;cursor:pointer}button:hover{border-color:var(--accent)}button.primary{background:var(--accent);color:#062015;border-color:var(--accent);font-weight:600}button.danger{border-color:#5a2a2a;color:var(--danger)}
#log{height:360px;overflow:auto;font:12px/1.5 ui-monospace,Consolas,monospace;color:#b9cadb}#log div{padding:3px 0;border-bottom:1px dashed #1b2836}#log .t{color:var(--accent2)}
.muted{color:var(--muted)}.toast{position:fixed;right:18px;bottom:18px;background:#152131;border:1px solid #2a3d52;padding:12px 16px;border-radius:12px;opacity:0;transform:translateY(10px);transition:.25s;pointer-events:none}.toast.show{opacity:1;transform:none}
</style></head><body>
<header><h1>Athena <span>GSM</span> · SIM7600</h1><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
<span class="badge"><span id="athenaDot" class="dot"></span><span id="athenaText">Athena</span></span>
<span class="badge"><span id="modemDot" class="dot"></span><span id="modemText">Модем</span></span>
<button id="tokenBtn" class="muted">Токен API</button></div></header>
<main><section style="display:grid;gap:18px">
<div class="card"><h2>Модем</h2><div class="grid" id="tiles"></div>
<div class="row"><input id="number" placeholder="+7 999 123-45-67" inputmode="tel"><button onclick="dial()">Позвонить</button><button onclick="act('/api/calls/answer')">Ответить</button><button class="danger" onclick="act('/api/calls/hangup')">Сбросить</button></div>
<div class="row"><input id="smsText" placeholder="Текст SMS"><button onclick="sms()">Отправить SMS</button><input id="ussd" placeholder="*100#" style="max-width:130px"><button onclick="ussd()">USSD</button></div>
<div class="row"><button onclick="act('/api/modem/reset')">Перезапустить модем</button></div></div>
<div class="card"><h2>Последние SMS</h2><div id="smsList" class="muted" style="font-size:13px">—</div></div>
</section><section><div class="card"><h2>События</h2><div id="log"></div></div></section></main>
<div class="toast" id="toast"></div>
<script>
const $=(id)=>document.getElementById(id);let token=localStorage.getItem('gwToken')||'';
function headers(){return token?{'Authorization':'Bearer '+token}:{}}
function toast(m,bad){const e=$('toast');e.textContent=m;e.style.borderColor=bad?'#5a2a2a':'#2a3d52';e.classList.add('show');clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('show'),3200)}
async function api(p,b){const o={method:b===undefined?'GET':'POST',headers:{...headers()}};if(b!==undefined){o.headers['Content-Type']='application/json';o.body=JSON.stringify(b)}const r=await fetch(p,o);const t=await r.text();let d=null;try{d=t?JSON.parse(t):null}catch(e){d=t}if(!r.ok)throw new Error((d&&d.detail)||r.statusText);return d}
async function act(p,b){try{await api(p,b);toast('Готово');setTimeout(refresh,500)}catch(e){toast(e.message||'Ошибка',true)}}
async function dial(){const n=$('number').value.trim();if(!n)return toast('Введите номер',true);await act('/api/dial',{number:n})}
async function sms(){const n=$('number').value.trim(),t=$('smsText').value.trim();if(!n||!t)return toast('Нужен номер и текст',true);await act('/api/sms',{number:n,text:t})}
async function ussd(){const c=$('ussd').value.trim();if(!c)return;try{const r=await api('/api/ussd',{code:c});toast(r.response||'USSD OK')}catch(e){toast(e.message||'Ошибка USSD',true)}}
$('tokenBtn').onclick=()=>{const v=prompt('API token (config bridge.api_token):',token);if(v!==null){token=v.trim();localStorage.setItem('gwToken',token);refresh();connect()}};
function tile(l,v,u=''){return `<div class="kv"><small>${l}</small><b>${v??'—'}${u}</b></div>`}
async function refresh(){try{const s=await api('/api/status');const m=s.modem||{},a=s.athena||{};
$('tiles').innerHTML=[tile('Состояние',m.state),tile('Оператор',m.operator||'—'),tile('Сигнал',m.signal_percent??'—','%'),tile('Сеть',m.network||'—'),tile('Регистрация',m.registered?'да':'нет'),tile('SIM',m.sim_state||'—'),tile('Звонок',m.call_active?'активен':'нет'),tile('SMS',(m.sms_sent||0)+' / '+(m.sms_received||0))].join('');
$('athenaDot').className='dot '+(a.connected?'on':'');$('athenaText').textContent=a.connected?'Athena: онлайн':'Athena: нет связи';
$('modemDot').className='dot '+(m.registered?'on':'');$('modemText').textContent=m.registered?'Модем: регистрация':'Модем: '+(m.state||'—');
const sms=await api('/api/sms/recent?limit=6').catch(()=>[]);$('smsList').innerHTML=sms.length?sms.map(x=>`<div style="padding:7px 0;border-bottom:1px dashed #1b2836"><b>${x.number||'—'}</b> <span class="muted">${x.direction==='out'?'→':'←'} ${x.at||''}</span><br>${(x.text||'').slice(0,110)}</div>`).join(''):'<span class="muted">Пока нет сообщений</span>' }catch(e){}}
let source;function connect(){if(source)source.close();const url='/api/events'+(token?'?token='+encodeURIComponent(token):'');source=new EventSource(url);source.onmessage=(e)=>{try{const r=JSON.parse(e.data);addLog(r.type,r.data)}catch(err){}};source.onerror=()=>{}}
function addLog(type,data){const rel=['call.incoming','call.state','sms.incoming','sms.sent','dtmf','device.event','command.received','modem.error','intercom.active','intercom.ended'];if(!rel.includes(type))return;let d='';
if(type==='call.incoming')d='звонок от '+data.number;else if(type==='call.state')d=`${data.state} ${data.number||''} ${data.duration_sec||''}`;else if(type==='sms.incoming')d=`SMS от ${data.number}: ${(data.text||'').slice(0,80)}`;else if(type==='sms.sent')d='SMS отправлена '+data.number;else if(type==='dtmf')d='DTMF '+data.digit;else if(type==='device.event')d=data.event+': '+JSON.stringify(data.data||{}).slice(0,100);else d=JSON.stringify(data).slice(0,120);
const l=document.createElement('div');l.innerHTML=`<span class="t">${new Date().toLocaleTimeString()}</span> <b>${type}</b> — ${d}`;const log=$('log');log.prepend(l);while(log.childNodes.length>120)log.removeChild(log.lastChild)}
refresh();connect();setInterval(refresh,5000);
</script></body></html>
"""
