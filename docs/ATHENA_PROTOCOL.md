# Протокол Athena (`device-protocol/1`)

Шлюз реализует клиентскую сторону протокола ядра. Источник истины —
`docs/device/` в репозитории Athena.

## Транспорт

- WebSocket: `ws(s)://<core>/api/device/ws`
- Текстовые кадры — JSON UTF-8, бинарные — PCM `s16le`, mono, 16 кГц,
  640 байт (20 мс). PCM используется и для intercom, и для голосового
  ассистента на GSM-звонке.
- Один endpoint = одна сессия. Новый `hello` заменяет старую (close `4000`).

## hello

```json
{
  "type": "hello",
  "token": "dev_...",
  "kind": "gsm",
  "firmware": "sim7600-gateway/1.2.4",
  "hardware": "SIM7600E-H",
  "capabilities": ["calls", "sms", "dtmf", "audio_in", "audio_out", "camera"],
  "device_id": "sim7600-gateway-01",
  "room_hint": "Прихожая",
  "ip": "192.168.1.50"
}
```

Токен обязан начинаться с `dev_`. Ошибка авторизации:

```json
{"type": "error", "code": "auth|revoked|not_found", "message": "..."}
```

`auth`/`not_found` — остановка, `revoked` — пауза 10 минут.

## welcome

Ядро отвечает `welcome` с `home`, `device`, `wake_words`, `audio`, `features`,
`server_time`, `pending_commands`. Затем досылает очередь команд в порядке
`(created_at, id)`.

## heartbeat

Каждые 20 с:

```json
{"type": "heartbeat", "status": "online", "rssi": -65, "uptime_sec": 3600, "ip": "192.168.1.50"}
```

`status="degraded"`, если модем не зарегистрирован или находится в `off`/`init`.
Ответ `heartbeat.ack` игнорируется. Три неудачных heartbeat → реконнект.

## Команды ядра

| kind | payload | Действие | ack |
|---|---|---|---|
| `call` | `{action:"dial", number, call_id}` | `ATD<number>;` | `done` после принятия модемом |
| `call` | `{action:"answer", number, call_id, mode?, bridge_room?, bridge_endpoint_id?}` | `ATA` + голосовой цикл или `call.bridge` | `done`; `failed`, если аудио недоступно |
| `call` | `{action:"hangup", number, call_id}` | `ATH`/`AT+CHUP` | `done`, затем `call.state ended` |
| `call` | `{number, reason:"reminder"/"automation", text}` | `ATD<number>;` | `done` |
| `sms` | `{number, text<=1000, person_id?}` | PDU (GSM-7/UCS-2, concat) | `done` после `+CMGS`/OK |
| `ussd` | `{code<=32}` | `AT+CUSD=1,"code",15` | `done` после ответа сети |
| `reboot` | `{}` | `recover()` модема | `done` |
| `config` | `{...}` | логирует, принимает | `done` |
| `intercom` | — | не команда; используйте `call.bridge` | `failed` |

Если модем выключен в конфиге, `call`/`sms`/`ussd`/`reboot` возвращают `failed`
с `modem is disabled in the gateway config`. Неизвестные kind —
`failed: unsupported command`, сессия остаётся живой.

### Ответ на звонок: assistant vs bridge

```json
{"type":"command","id":42,"kind":"call","payload":{
  "call_id":7,"number":"+79991234567","action":"answer",
  "mode":"assistant","bridge_room":null,"bridge_endpoint_id":null
}}
```

- `mode="assistant"` (по умолчанию) — после `ATA` запускается голосовой цикл:
  `audio.start` → PCM → `audio.end` → `assistant.reply` + `audio.url` →
  `playback.stopped` → следующий `audio.start follow_up:true` или
  `conversation.end`.
- `mode="bridge"` (или задан `bridge_room`/`bridge_endpoint_id`) — после `ATA`
  отправляется `call.bridge`; PCM идёт в intercom-сессию.
- Аудио обязательно: без `[audio_bridge].enabled` и serial PCM/ALSA (либо mock) команда
  получает `failed: voice audio is unavailable...`, звонок не поднимается.
- Если bridge не удался, но звонок активен, шлюз отвечает
  `{"status":"done","mode":"assistant","warning":"..."}` и продолжает как
  ассистент; если звонок уже завершён — `failed`.

Подробный сценарий — [CALL_VOICE.md](CALL_VOICE.md).

### Формат ack

```json
{"type":"command.ack","id":42,"status":"done"}
{"type":"command.ack","id":43,"status":"failed","error":"modem rejected SMS"}
```

Статусы: `delivered`, `done`, `failed`. Только `done` фиксирует завершение
команды в ядре. Ack идемпотентен. Шлюз хранит `last_command_id` в
`data/athena_state.json`: повторный id не выполняется второй раз.

## События шлюза

| type | data | Комментарий |
|---|---|---|
| `call.incoming` | `{number, at}` | До `ATA`, один раз на звонок |
| `call.state` | `{state, number, duration_sec?}` | `active`, `ended`, `missed`, `failed` |
| `sms.incoming` | `{number, text<=2000}` | Полный текст после склейки UDH |
| `dtmf` | `{digit}` | Только реальный DTMF абонента |
| `device.event` | `{event, data}` | `ussd.result`, `diag.modem`, `camera.*` |
| `call.bridge` | `{room?/target_endpoint_id?, call_number?}` | Спецкадр после ответа |
| `playback.stopped` | `{reason}` | Ответ проигран в трубку |
| `conversation.end` | `{reason}` | Голосовой диалог закрыт (`ended`/`timeout`/`error`) |

SSE-события шлюза дополнительно включают `bridge.error`, `core.error`,
`intercom.frame`, `modem.*`, `camera.*`, но в Athena уходят только кадры
протокола.

Успешная обработка события → `{"type":"event.ack","event":"<type>"}`.
Ошибка → `{"type":"error","code":"event_failed"}`. События не переотправляются
после реконнекта (нет идемпотентности).

### Последовательности звонка

Входящий, отвечен:

```
call.incoming → call.state active → call.state ended
```

Пропущенный: `call.incoming → call.state missed (duration_sec=0)`.
Исходящий: `(call создан ядром) → call.state active → call.state ended`.

### Голосовой цикл (assistant)

```
audio.start {utterance_id,sample_rate:16000,format:"pcm_s16le",follow_up:false}
<PCM 640 B>
audio.end {utterance_id}
assistant.reply {text,listen,end,follow_up_timeout_seconds}
audio.url {url,format,duration}
playback.stopped {reason}
audio.start {...,follow_up:true}   # если listen=true и end=false
...
conversation.end {reason:"ended"|"timeout"|"error"}
```

Шлюз ждёт `assistant.reply` до 45 с, follow-up — не меньше 3 с. При отсутствии
`audio.url` используется `POST /api/device/tts`.

## call.bridge (телефон ↔ колонка)

1. Шлюз принимает звонок (`ATA`) и при `hold_before_bridge = true` удерживает
   линию (`AT+CHLD=2`). По умолчанию `hold_before_bridge = false`.
2. Отправляет:

```json
{"type":"call.bridge","room":"Гостиная","call_number":"+79991234567"}
```

3. Ядро создаёт intercom-сессию `mode=bridge`, отправляет колонке
   `intercom.invite`, шлюзу — `intercom.ringing {session_id}`.
4. Шлюз ждёт `event.ack` для `call.bridge`; при отсутствии — таймаут
   подтверждения и fallback.
5. Колонка отвечает `intercom.answer`; обоим приходит `intercom.active`.
6. Шлюз запускает arecord/aplay и шлёт PCM 16 кГц / 640 байт.
7. При завершении — `intercom.end {session_id, reason:"hangup"}`.

Если колонка отклонила вызов, обычный разговор остаётся живым (или
переключается на ассистента при ошибке bridge).

## Реконнект

| Событие | Поведение |
|---|---|
| Close `4000` | Не переподключаться: endpoint занят |
| Close `1008` / revoked | Пауза 10 минут |
| Сеть/таймаут | Backoff 1→2→4→8→16→30→60 с + jitter 0–20 % |
| `welcome` | Backoff сбрасывается |

## Ограничения v1

- Один звонок одновременно (`ringing` тоже занимает модем).
- `unknown_callers` — декларативная настройка ядра; отклонение выполняет шлюз.
- Политика ядра для `call.bridge` не проверяется (GSM-endpoint доверенный).
- Запись разговоров не ведётся; PCM живёт только во время активного звонка
  (assistant voice) или intercom-сессии.
- Голосовой ассистент требует ALSA/USB-аудио; без него доступны только
  сигнализация, SMS и raw `ATA` (тишина в линии).
