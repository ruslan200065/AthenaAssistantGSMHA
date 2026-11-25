# Голосовой звонок: «позвонить Афине»

Шлюз умеет не просто поднимать трубку, а отдавать звонок ядру Athena как
голосовому устройству: абонент говорит, ядро отвечает, ответ проигрывается в
трубку. Wake word не нужен — для endpoint `kind="gsm"` каждое принятое
обращение считается отдельной репликой диалога.

```
Абонент ──GSM──► SIM7600 ──USB audio──► arecord ──VAD──► audio.start + PCM
                                                            │
Athena Core ◄───────────────────────────────────────────────┘
      │ assistant.reply + audio.url
      ▼
aplay ──► SIM7600 ──GSM──► Абонент
```

## Условия

- `[audio_bridge].enabled = true`;
- сырой PCM-порт модема `/dev/ttyUSB4` (опция `pcm_port`, в HA add-on —
  `pcm_port`), либо ALSA-устройства (`arecord -l`, `aplay -l`) как fallback;
- `modem.mock` для отладки без железа;
- `audio_transport = "auto"` сначала пробует serial PCM, затем ALSA;
- исходящий вызов должен быть принят **командой ядра** `call {action:"answer"}`.
  Локальный REST `POST /api/calls/answer` делает только `ATA` и не запускает
  голосовой цикл — в трубке будет тишина.

## 1. Ответ на звонок

Ядро присылает:

```json
{
  "type": "command",
  "id": 42,
  "kind": "call",
  "payload": {
    "call_id": 7,
    "number": "+79991234567",
    "action": "answer",
    "mode": "assistant",
    "bridge_room": null,
    "bridge_endpoint_id": null
  }
}
```

Шлюз:

1. Проверяет доступность аудио. Если аудио нет — `command.ack` со статусом
   `failed` и текстом `voice audio is unavailable (audio_bridge.enabled/ALSA)`,
   звонок не поднимается.
2. Запускает голосовую сессию (`CallVoiceSession`).
3. Выполняет `ATA`, отправляет `call.state {"state":"active","number":"..."}`.
4. Отвечает `command.ack {"status":"done"}`.

`mode`:

| mode | Поведение |
|---|---|
| `assistant` (по умолчанию) | Абонент говорит с Афиной; PCM идёт в ядро |
| `bridge` | После `ATA` шлётся `call.bridge`; PCM идёт в intercom-сессию с колонкой |

Если `bridge_room`/`bridge_endpoint_id` заданы, режим считается bridge даже при
`mode:"assistant"`.

### Ошибки и fallback

- **Аудио недоступно до ответа** — ack `failed`, звонок не принимается.
- **Bridge не удался, звонок ещё активен** — шлюз возвращает ack
  `{"status":"done","mode":"assistant","warning":"..."}` и продолжает как
  голосовой ассистент, чтобы абонент не попал в тишину.
- **Bridge не удался и звонок уже завершён** — ack `failed`.
- **`ATA` не удался** — голосовая сессия останавливается, ack `failed`.

## 2. Слушаем абонента

`CallVoiceSession` открывает `arecord` на 16 кГц, mono, `pcm_s16le`.
Кадр — 640 байт (20 мс). Локальный VAD:

- адаптивный порог по шуму;
- pre-roll ~300 мс, чтобы не потерять начало фразы;
- конец фразы после ~700 мс тишины;
- максимальная фраза — 20 с.

Перед отправкой речи:

```json
{
  "type": "audio.start",
  "utterance_id": "gsm-1750000000-1",
  "sample_rate": 16000,
  "format": "pcm_s16le",
  "follow_up": false
}
```

Затем бинарные PCM-кадры по 640 байт и:

```json
{"type": "audio.end", "utterance_id": "gsm-1750000000-1"}
```

Ядро принимает до 120 с / 25 МиБ на utterance. Для `kind="gsm"` wake word не
проверяется.

## 3. Ответ в трубку

После обработки ядро присылает:

```json
{
  "type": "assistant.reply",
  "text": "Привет! Чем помочь?",
  "conversation_id": 12,
  "listen": true,
  "end": false,
  "follow_up_timeout_seconds": 8,
  "duration_hint": 3.4
}
```

и `audio.url`:

```json
{
  "type": "audio.url",
  "utterance_id": "reply-12-1",
  "url": "http://core:8000/media/reply.ogg",
  "format": "audio/ogg",
  "duration": 3.4,
  "text": "Привет! Чем помочь?",
  "can_interrupt": false
}
```

Шлюз:

1. Скачивает файл (лимит 25 МиБ) или, если URL недоступен, запрашивает TTS:
   `POST /api/device/tts` с `X-Device-Token` и телом `{"text": ...}`.
2. Декодирует в PCM 16 кГц через ffmpeg (OGG/MP3/WAV).
3. Проигрывает в `aplay` с реальным темпом, не дольше `duration` (если оно
   задано), на время проигрывания микрофон приглушается.
4. Отправляет `playback.stopped {reason}`.

Если ядро не прислало ни `audio.url`, ни текст для TTS, сессия завершает
диалог ошибкой `assistant.error`.

## 4. Продолжение диалога

Если в `assistant.reply` `end=false` и `listen=true`:

- шлюз снова слушает и отправляет следующую фразу с
  `audio.start {follow_up: true, ...}`;
- таймаут ожидания следующей фразы — `follow_up_timeout_seconds` (минимум 3 с,
  по умолчанию 8 с);
- по таймауту или при `end=true` отправляется
  `{"type":"conversation.end","reason":"timeout"|"ended"}`.

Ответ ядра ожидается до 45 с; иначе — `assistant.error` и завершение диалога.

## 5. Завершение звонка

При `ATH`/`NO CARRIER`/`VOICE CALL: END`:

- голосовая сессия останавливается, микрофон и динамик закрываются;
- в ядро уходит `call.state {"state":"ended","number":"...","duration_sec":N}`;
- при активном bridge — `intercom.end {session_id, reason:"hangup"}`.

Если этого не сделать, звонок в ядре останется `active` и заблокирует следующие
вызовы.

## 6. Режим bridge (телефон ↔ колонка)

1. Ядро присылает `call {action:"answer", mode:"bridge", bridge_room:"Кухня"}`
   (или `bridge_endpoint_id`).
2. Шлюз проверяет аудио, отвечает `ATA`.
3. Отправляет `call.bridge {room|target_endpoint_id, call_number}`.
4. Ядро создаёт intercom-сессию, шлёт `intercom.ringing` (с `session_id`),
   колонка отвечает → обоим `intercom.active`.
5. Шлюз подтверждает `event.ack` для `call.bridge`; при отсутствии ответа
   срабатывает таймаут подтверждения.
6. PCM 16 кГц / 640 байт идёт между GSM-линией и intercom.
7. По завершении — `intercom.end`.

Отказ колонки/ошибка bridge не должны оставлять абонента в тишине: шлюз
переключается на голосового ассистента (если звонок ещё активен) либо кладёт
трубку.

## 7. Конфигурация

```toml
[modem]
port = "/dev/ttyUSB2"

[audio_bridge]
enabled = true
pcm_port = "/dev/ttyUSB4"
pcm_baudrate = 115200
audio_transport = "auto"
playback_volume = 50          # громкость ответов в трубку, 0..200
capture_device = "auto"
playback_device = "auto"
sample_rate = 16000
chunk_ms = 20
hold_before_bridge = false
```

В HA add-on те же поля задаются опциями `audio_enabled`, `pcm_port`
(обычно `/dev/ttyUSB4`), `audio_device` (ALSA fallback) и `playback_volume`
(по умолчанию 50, как в рабочем референсе).

Ядро присылает ровно один `assistant.reply` и один `audio.url` на реплику
(один `utterance_id`). Шлюз дополнительно дедуплицирует `audio.url` по
`utterance_id` и не запускает TTS-fallback, если URL уже пришёл или играет:
повтор фразы в трубку исключён. Воспроизведение идёт по deadline-расписанию
(20 мс на кадр), поэтому не «плывёт» и не заикается.

## 8. Диагностика

`GET /api/status → voice_call`:

```json
{
  "active": true,
  "playing": false,
  "listening": true,
  "speaking": false,
  "frames_in": 390,
  "frames_out": 172,
  "last_error": null,
  "sample_rate": 16000
}
```

`GET /api/diagnostics` дополнительно отдаёт AT-журнал и состояние модема.
Типичные проблемы:

- «тишина в трубке» — `audio_bridge.enabled=false`, неверное ALSA-устройство или
  звонок поднят через REST, а не командой ядра;
- эхо/обрывы — проверьте `alsamixer`, уровни capture/playback, `AT+CPCMREG=1`;
- речь не распознаётся — VAD не слышит абонента: проверьте `arecord -D ... -f
  S16_LE -r 16000 -c 1 -t raw | aplay`, затем `frames_in` в статусе.

## 9. Связанные документы

- [Протокол Athena](ATHENA_PROTOCOL.md) — кадры `audio.*`, `assistant.reply`,
  `call.bridge`, `intercom.*`.
- [API](API.md) — REST-эндпоинты и ограничения.
- [Диагностика](TROUBLESHOOTING.md) — ALSA и модем.
