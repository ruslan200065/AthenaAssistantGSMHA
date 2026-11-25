# Голосовой звонок: «позвонить Афине»

Шлюз работает как обычное голосовое устройство Athena без wake-слова: человек
звонит на SIM-номер, ядро поднимает трубку (`mode: "assistant"`), и дальше идёт
обычный цикл `audio.start → PCM → audio.end → assistant.reply → audio.url`.

## Что делает шлюз

### 1. Ответ на звонок

Команда ядра:

```json
{"type":"command","kind":"call","payload":{
  "call_id":42,"number":"+79991234567","action":"answer",
  "mode":"assistant","bridge_room":null,"bridge_endpoint_id":null}}
```

Шлюз:

1. `ATA`;
2. `command.ack {"status":"done"}`;
3. `call.state {"state":"active","number":"..."}`;
4. запускает голосовой цикл (если `[audio_bridge].enabled`).

В режиме `mode: "bridge"` вместо этого после `ATA` отправляется `call.bridge`
с `bridge_room`/`bridge_endpoint_id`, а PCM идёт в intercom-сессию.

### 2. Слушаем абонента

- `arecord` с USB-аудио SIM7600: **16 кГц, mono, s16le**, кадры по 640 байт
  (20 мс).
- Локальный VAD: адаптивный порог по шуму линии, pre-roll 300 мс, конец фразы
  после ~700 мс тишины, максимум 20 с.
- При начале речи:

```json
{"type":"audio.start","utterance_id":"gsm-…","sample_rate":16000,
 "format":"pcm_s16le","follow_up":false}
```

- дальше бинарные PCM-кадры по 640 байт;
- при паузе:

```json
{"type":"audio.end","utterance_id":"gsm-…"}
```

Ядро распознаёт речь, вызывает ассистента и присылает `assistant.reply` +
`audio.url`. Wake-слово не используется: для `kind="gsm"` ядро принимает каждую
фразу как реплику.

### 3. Отвечаем в трубку

`assistant.reply` содержит `listen`, `end`, `follow_up_timeout_seconds`,
`duration_hint`. Затем приходит:

```json
{"type":"audio.url","utterance_id":"gsm-…","url":"https://core/…/tts.ogg",
 "format":"audio/ogg","duration":4.2,"text":"…","can_interrupt":true}
```

Шлюз:

1. скачивает файл (до 25 МиБ, таймаут 20 с);
2. декодирует `ffmpeg` в PCM 16 кГц mono;
3. пишет в `aplay` в темпе реального времени, не дольше `duration`;
4. на время проигрывания глушит микрофон (half-duplex, чтобы не услышать себя);
5. отправляет `playback.stopped {"reason":"done"}`.

Если `audio.url` нет (TTS ядра недоступен), шлюз пробует
`POST /api/device/tts` с `X-Device-Token` и играет полученный PCM.

### 4. Продолжение диалога

Если в `assistant.reply` `listen: true` и `end: false`, после проигрывания шлюз
снова открывает микрофон. Следующая фраза уходит с:

```json
{"type":"audio.start","utterance_id":"gsm-…","sample_rate":16000,
 "format":"pcm_s16le","follow_up":true}
```

Если тишина дольше `follow_up_timeout_seconds` (минимум 3 с), шлюз отправляет
`conversation.end {"reason":"timeout"}`.

Если `listen: false` / `end: true` — после ответа сразу
`conversation.end {"reason":"ended"}`.

### 5. Завершение звонка

`NO CARRIER`, `VOICE CALL: END`, `MISSED_CALL` или watchdog `AT+CLCC` →
`call.state ended/missed`; голосовой цикл и ALSA-процессы останавливаются.

## Настройка ALSA

В конфиге:

```toml
[audio_bridge]
enabled = true
capture_device = "auto"     # или "plughw:1,0"
playback_device = "auto"
sample_rate = 16000
chunk_ms = 20
hold_before_bridge = true
```

`auto` ищет карту с `SIM7600` в `arecord -l`. Проверка на хосте:

```bash
arecord -l
aplay -l
# тест записи/воспроизведения
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay -D plughw:1,0 /tmp/test.wav
```

Если модем отдаёт 8 кГц, оставьте `sample_rate = 16000` только при поддержке
картой; иначе укажите фактическую частоту (ядро принимает `sample_rate` из
`audio.start`, но не ресемплирует). Для SIM7600 обычно доступны 8/16/48 кГц.

## Ограничения v1

- Half-duplex: во время ответа микрофон закрыт, barge-in не поддерживается.
- Нет AEC: эхо подавляется паузой записи и echo-window ядра.
- PCM вниз (`audio.start` от ядра) не используется — ядро v1 присылает
  `audio.url`.
- Аудиомост `mode: "bridge"` и голосовой цикл взаимоисключающие.

## Диагностика

`GET /api/status` → `voice_call`:

```json
{"active":true,"playing":false,"listening":true,"speaking":false,
 "frames_in":120,"frames_out":340,"last_error":null}
```

Логи: `audio.call` (VAD, audio.start/end, воспроизведение) и `bridge`
(команда `call` с mode).
