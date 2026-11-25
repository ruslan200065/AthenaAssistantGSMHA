# REST API шлюза

База: `http://<host>:8099`. Авторизация: `Authorization: Bearer <bridge.api_token>`
или `X-Api-Token: <token>`. Для потоковых GET допускается `?token=...`.

Ошибки: `{"detail": "..."}` с кодом 400/401/403/404/503.

## Состояние

### `GET /api/health` (без токена)

```json
{"status":"ok","version":"1.0.0","uptime_sec":120,"mock":false}
```

### `GET /api/status`

```json
{
  "gateway": {"name":"GSM-шлюз SIM7600","version":"1.0.0","uptime_sec":120,"mock":false,"time":"...","config_source":"config.toml"},
  "modem": {
    "state":"idle","online":true,"sim_ready":true,"sim_state":"READY",
    "registered":true,"registration_status":1,"registration_text":"registered_home",
    "signal_dbm":-65,"signal_percent":77,"operator":"MTS","network":"LTE",
    "model":"SIM7600E-H","firmware":"...","imei":"...","iccid":"...",
    "phone_number":"+79990001122","call_active":false,"ringing":false,
    "caller":null,"last_error":null,"uptime_sec":120,
    "sms_sent":2,"sms_received":5,"updated_at":1760000000.0
  },
  "camera": {"device":"/dev/video0","mock":false,"width":1280,"height":720,"fps":15,
             "pixel_format":"MJPG","available":true,"frames":900,"subscribers":1,
             "motion_detection":true,"motion_events":3,"snapshots":[...]},
  "athena": {"connected":true,"fatal":null,"last_error":null,"url":"ws://...",
             "home":"Дом","device":"GSM","room":"Прихожая","features":{...},
             "reconnect_attempt":0,"last_command_id":42},
  "audio": {"enabled":false,"active":false,"frames_in":0,"frames_out":0},
  "stats": {"sms_in_log":3,"calls_in_log":2,"intercom_session":null}
}
```

### `GET /api/diagnostics`

Тот же статус плюс последние 50 AT-обменов (номера маскированы), настройки
модема/камеры и состояние Athena.

## SMS

### `POST /api/sms`

```json
{"number": "+79991234567", "text": "Привет"}
```

Ответ: `{"ok": true, "parts": 1}`. Длинный текст автоматически делится на части.

### `GET /api/sms/recent?limit=50`

Последние входящие/исходящие SMS из памяти процесса (не из модема).

## USSD

### `POST /api/ussd`

```json
{"code": "*100#"}
```

```json
{"ok": true, "code": "*100#", "status": 0, "response": "Баланс: 350 ₽"}
```

Таймаут ответа сети — 30 с (503).

### `GET /api/ussd/recent`

## Звонки

| Метод | Тело | Действие |
|---|---|---|
| POST `/api/dial` | `{"number":"+7..."}` | `ATD` |
| POST `/api/calls/answer` | — | `ATA` |
| POST `/api/calls/hangup` | — | `ATH` |
| POST `/api/calls/dtmf` | `{"digits":"1#"}` | `AT+VTS` |
| POST `/api/calls/bridge` | `{"room":"Гостиная"}` или `{"endpoint_id":"..."}` | ответить (если ringing) + `call.bridge` в ядро |
| GET `/api/calls/recent` | — | журнал звонков |

`/api/calls/bridge` вернёт 503, если Athena не подключена.

## Модем

### `POST /api/modem/reset`

Планирует `recover()` (AT+CFUN / GPIO / переоткрытие порта). Ответ приходит
сразу: `{"ok": true, "scheduled": true}`.

### `POST /api/modem/at`

Только диагностический allowlist: `AT`, `ATI`, `AT+CSQ`, `AT+CREG?`, `AT+CGREG?`,
`AT+COPS?`, `AT+CPIN?`, `AT+CGSN`, `AT+CCID`, `AT+CGMR`, `AT+GMM`, `AT+CPSI?`,
`AT+CLCC`. Иначе 403.

```json
{"command": "AT+CSQ"}
→ {"ok": true, "final": "OK", "lines": ["+CSQ: 24,99"]}
```

## Камера

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/api/camera/info` | метаданные, счётчики, последние снимки |
| GET | `/api/camera/snapshot.jpg` | `image/jpeg`, 503 без кадра |
| GET | `/api/camera/snapshot.jpg?save=true` | + заголовок `X-Snapshot-File` |
| GET | `/api/camera/stream.mjpg` | `multipart/x-mixed-replace; boundary=athena-frame` |
| POST | `/api/camera/record` | `{"seconds":12}` → `{path, filename, url}` |
| GET | `/api/camera/clips/{name}` | `video/mp4` |
| GET | `/api/camera/snapshots` | список снимков |

## События (SSE)

### `GET /api/events`

```
retry: 3000

data: {"seq":12,"type":"sms.incoming","data":{"number":"+7999...","text":"..."},"at":1760000000.1}

: keepalive
```

Формат — только `data:` (JSON), без named events — удобно для `EventSource`.
Keepalive-комментарий каждые 15 с. Рекомендуется `curl -N` или HA-интеграция.

### `GET /api/events/recent?limit=50&type=call.state`

История событий из памяти (до 200).

Типы: `gateway.start`, `gateway.stop`, `modem.ready`, `modem.error`,
`call.incoming`, `call.state`, `call.dialing`, `sms.incoming`, `sms.sent`,
`dtmf`, `device.event`, `command.received`, `camera.motion`, `camera.snapshot`,
`intercom.ringing`, `intercom.active`, `intercom.ended`.

## Симулятор (только `modem.mock=true`)

| Метод | Тело |
|---|---|
| POST `/api/dev/call` | `{"number":"+7..."}` |
| POST `/api/dev/sms` | `{"number":"+7...","text":"..."}` |
| POST `/api/dev/dtmf` | `{"digits":"5"}` |
| POST `/api/dev/hangup` | — |

## Коды ошибок

| Код | Причина |
|---|---|
| 400 | неверное тело/формат |
| 401 | нет или неверный токен |
| 403 | команда вне allowlist |
| 404 | нет клипа |
| 503 | модем занят/не готов, нет кадра, USSD без ответа, Athena offline |
