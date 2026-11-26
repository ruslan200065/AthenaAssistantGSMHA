# REST API шлюза

База: `http://<host>:8099`. Авторизация: `Authorization: Bearer <bridge.api_token>`
или `X-Api-Token: <token>`. Токен в query (`?token=...`) принимается только для

Ошибки: `{"detail": "..."}` с кодом 401/403/404/422/503. Pydantic-валидация
тела запроса возвращает 422.

## Состояние

### `GET /api/health` (без токена)

```json
{"status":"ok","version":"1.3.2","uptime_sec":120,"mock":false}
```

### `GET /api/status`

```json
{
  "gateway": {"name":"GSM-шлюз SIM7600","version":"1.3.2","uptime_sec":120,"mock":false,"time":"...","config_source":"config.toml"},
  "modem": {
    "state":"idle","online":true,"sim_ready":true,"sim_state":"READY",
    "registered":true,"registration_status":1,"registration_text":"registered_home",
    "signal_dbm":-65,"signal_percent":77,"operator":"MTS","network":"LTE",
    "model":"SIM7600E-H","firmware":"...","imei":"...","iccid":"...",
    "phone_number":"+79990001122","call_active":false,"ringing":false,
    "caller":null,"last_error":null,"uptime_sec":120,
    "sms_sent":2,"sms_received":5,"updated_at":1760000000.0
  },
             "pixel_format":"MJPG","available":true,"frames":900,"subscribers":1,
  "athena": {"connected":true,"fatal":null,"last_error":null,"url":"ws://...",
             "home":"Дом","device":"GSM","room":"Прихожая","features":{...},
             "reconnect_attempt":0,"last_command_id":42},
  "audio": {"enabled":true,"active":false,"frames_in":0,"frames_out":0},
  "voice_call": {"active":false,"playing":false,"listening":false,"speaking":false,
                 "frames_in":0,"frames_out":0,"last_error":null,"sample_rate":16000},
  "pcm_enabled": false,
  "stats": {"sms_in_log":3,"calls_in_log":2,"intercom_session":null}
}
```

`pcm_enabled` — включён ли PCM-маршрут модема (`AT+CPCMREG=1`) для текущего
голосового звонка или моста.

### `GET /api/diagnostics`

Тот же статус плюс последние 50 AT-обменов (номера маскированы), настройки

## SMS

### `POST /api/sms`

```json
{"number": "+79991234567", "text": "Привет"}
```

Ответ: `{"ok": true, "parts": 1}`. Длинный текст автоматически делится на части.
Ограничения: номер 3–32 символа, текст 1–1000 символов (иначе 422).

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

Таймаут ответа сети — 30 с (503). Код: 1–32 символа из `0-9*#+`.

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

`POST /api/calls/answer` — «сырой» `ATA`: трубка поднимается, но голосовой
ассистент **не** запускается, абонент услышит тишину. Голосовой сценарий
запускается только командой ядра `call {action:"answer", mode:"assistant"}` или
`mode:"bridge"` (см. [CALL_VOICE.md](CALL_VOICE.md)).

`/api/calls/bridge` требует доступного аудио (`[audio_bridge].enabled` +
serial PCM-порт `pcm_port` или ALSA,
либо mock) и подключённой Athena. Отвечает на звонок, если он ещё ringing, и
отправляет `call.bridge`; при недоступности аудио/ядра — 503. Ответ
`{"ok": true, "room": "...", "target_endpoint_id": ...}` означает, что кадр
`call.bridge` отправлен, а не что колонка уже ответила.

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
`intercom.ringing`, `intercom.active`, `intercom.ended`, `intercom.frame`,
`bridge.error`, `core.error`.

Голосовые события (`audio.start`, `audio.end`, `assistant.reply`, `audio.url` (части),
`audio.done`, `playback.stopped`, `conversation.end`) идут в WebSocket ядра и в диагностику;
в SSE публикуются только служебные статусы `voice.*` при их наличии.

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
| 401 | нет или неверный токен |
| 403 | токен в query для не-streaming пути; команда вне allowlist |
| 404 | нет клипа |
| 422 | тело запроса не прошло валидацию Pydantic |
| 503 | модем занят/не готов/выключен, нет кадра, USSD без ответа, Athena offline, аудио для голосового звонка недоступно |
