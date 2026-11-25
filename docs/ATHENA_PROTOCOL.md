# Протокол Athena (`device-protocol/1`)

Шлюз реализует клиентскую сторону протокола ядра. Источник истины —
`docs/device/` в репозитории Athena.

## Транспорт

- WebSocket: `ws(s)://<core>/api/device/ws`
- Текстовые кадры — JSON UTF-8, бинарные — PCM (только intercom).
- Один endpoint = одна сессия. Новый `hello` заменяет старую (close `4000`).

## hello

```json
{
  "type": "hello",
  "token": "dev_...",
  "kind": "gsm",
  "firmware": "sim7600-gateway/1.0.0",
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

Шлюз различает: `auth`/`not_found` — стоп, `revoked` — пауза 10 минут.

## welcome

Ядро отвечает `welcome` с `home`, `device`, `wake_words`, `audio`,
`features`, `server_time`, `pending_commands`. После `welcome` ядро досылает
очередь команд в порядке `(created_at, id)`.

Шлюз логирует дом/устройство, сбрасывает буферы и начинает heartbeat.

## heartbeat

Каждые 20 с:

```json
{"type": "heartbeat", "status": "online", "rssi": -65, "uptime_sec": 3600, "ip": "192.168.1.50"}
```

`status="degraded"`, если модем не зарегистрирован в сети. Ответ
`{"type":"heartbeat.ack","at":"..."}` игнорируется по содержимому.
Три неудачных heartbeat → сокет закрывается, запускается реконнект.

## Команды ядра

| kind | payload | Что делает шлюз | ack |
|---|---|---|---|
| `call` | `{action:"dial", number, call_id}` | `ATD<number>;` | `done` после OK/ATH-принятия |
| `call` | `{action:"answer", number, call_id}` | `ATA` (идемпотентно) | `done`, затем `call.state active` |
| `call` | `{action:"hangup", number, call_id}` | `ATH`/`AT+CHUP` | `done`, опционально `call.state ended` |
| `call` | `{number, reason:"reminder", text}` | `ATD<number>;` | `done` |
| `sms` | `{number, text<=1000, person_id?}` | PDU-отправка (GSM-7/UCS-2, concat) | `done` после `+CMGS`/OK |
| `ussd` | `{code<=32}` | `AT+CUSD=1,"code",15` | `done` после ответа сети |
| `reboot` | `{}` | `recover()` модема | `done` |
| `config` | `{...}` | логирует, принимает | `done` |
| `intercom` | — | не поддерживается как команда | `failed` с пояснением |

Всё остальное (`announce`, `tts`, `play`, `display`, `led`, `ota`, …) получает
`failed: unsupported command`, сессия остаётся живой.

### Формат ack

```json
{"type": "command.ack", "id": 42, "status": "done"}
{"type": "command.ack", "id": 43, "status": "failed", "error": "modem rejected SMS"}
```

`id` — целое из команды. Ack идемпотентен. Шлюз хранит
`last_command_id` в `data/athena_state.json`; повторный id ≤ сохранённого
получает прежний статус без повторного выполнения (защита от двойного звонка
или SMS после реконнекта).

## События шлюза

| type | data | Комментарий |
|---|---|---|
| `call.incoming` | `{number, at}` | Отправляется до `ATA`, один раз на звонок |
| `call.state` | `{state, number, duration_sec?}` | `active`, `ended`, `missed`, `failed` |
| `sms.incoming` | `{number, text<=2000}` | Полный текст после склейки UDH |
| `dtmf` | `{digit}` | Только реальный DTMF абонента (`+DTMF`/`+RXDTMF`) |
| `device.event` | `{event, data}` | `ussd.result`, `diag.modem`, `camera.motion`, `camera.snapshot` |
| `call.bridge` | `{room?/target_endpoint_id?, call_number?}` | Спецкадр после ответа на звонок |

Успешная обработка → `{"type":"event.ack","event":"<type>"}`. Ошибка →
`{"type":"error","code":"event_failed",...}`. События **не** переотправляются
после реконнекта, чтобы не создавать дубликаты звонков и уведомлений.

### Последовательности

Входящий, отвечен:

```
call.incoming → call.state active → call.state ended
```

Пропущенный:

```
call.incoming → call.state missed (duration_sec=0)
```

Исходящий:

```
(call создан ядром) → call.state active → call.state ended
```

### USSD

Ответ сети отправляется как:

```json
{"type":"device.event","event":"ussd.result","data":{"code":"*100#","response":"Баланс: 350 ₽"}}
```

Одновременно ack команды `ussd` = `done`.

## call.bridge (телефон ↔ колонка)

1. Шлюз принимает входящий звонок (`ATA`), линия удерживается (`AT+CHLD=2`).
2. Отправляет:

```json
{"type":"call.bridge","room":"Гостиная","call_number":"+79991234567"}
```

3. Ядро создаёт intercom-сессию `mode=bridge`, отправляет колонке
   `intercom.invite`, шлюзу — `intercom.ringing` с `session_id`.
4. Колонка отвечает `intercom.answer`; обоим приходит `intercom.active`.
5. Шлюз (если `[audio_bridge].enabled`) запускает arecord/aplay и шлёт бинарный
   PCM 16 кГц / 20 мс (640 байт), входящий PCM пишет в линию.
6. При завершении звонка шлюз отправляет
   `{"type":"intercom.end","session_id":N,"reason":"hangup"}`.

Если колонка отклонила вызов, обычный телефонный разговор остаётся живым.

## Реконнект

| Событие | Поведение |
|---|---|
| Close `4000` | Не переподключаться: endpoint занят новой сессией |
| Close `1008` / revoked | Пауза 10 минут, затем попытка |
| Сеть/таймаут | Backoff 1→2→4→8→16→30→60 с + jitter 0–20 % |
| `welcome` получен | Backoff сбрасывается, очередь команд ядра принимается |

## Ограничения v1

- Один звонок одновременно (`ringing` тоже занимает модем).
- `unknown_callers` — декларативная настройка ядра; фактическое отклонение
  выполняет шлюз.
- Ядро не проверяет политику для `call.bridge` (GSM-endpoint доверенный).
- Аудио запись не ведётся; PCM живёт только в активной intercom-сессии.
