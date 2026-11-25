# Athena SIM7600 Gateway (Home Assistant app)

HA-приложение, которое поднимает шлюз SIM7600 + USB-камеры прямо на хосте Home
Assistant. Интеграция `athena_sim7600` из корня репозитория подключается к нему по
`http://<HA-хост>:8099`.

## Установка

1. «Настройки → Дополнения (Приложения) → Магазин → ⋮ → Репозитории».
2. Добавьте `https://github.com/ruslan200065/AthenaAssistantGSMHA`.
3. Установите **Athena SIM7600 Gateway**, откройте «Настройки»:
   - `athena_url` / `athena_token` — ядро Athena (можно оставить пустым);
   - `api_token` — токен для интеграции HA;
   - `modem_port` — обычно `/dev/ttyUSB2`;
   - `camera_device` — `/dev/video0`;
   - `modem_mock` / `camera_mock` — включите для проверки без железа.
4. Запустите приложение. Проверьте «Журнал» и `http://<HA-хост>:8099/api/health`.
5. Опционально: добавьте интеграцию **Athena SIM7600 GSM** (вручную из
   `custom_components/`), если нужны сенсоры/кнопки/сервисы в HA. Для камеры
   достаточно встроенной **Generic Camera** (ниже).

Приложение работает с `host_network: true`, поэтому порт 8099 доступен напрямую,
а camera/GSM/audio устройства проброшены через `uart`, `usb`, `video`, `audio`.

## Голосовой звонок (разговор с Афиной)

Опция `audio_enabled` включает голосовой цикл. Основной путь — сырой PCM-порт
модема `/dev/ttyUSB4` (опция `pcm_port`). Если прошивка отдаёт звук как USB-карту,
используется ALSA: `audio_device: "auto"` ищет SIM7600 через `arecord -l`, либо
укажите вручную, например `plughw:1,0`. Громкость ответов в трубку задаёт
`playback_volume` (0–200, по умолчанию 50 — как в рабочем референсе).

После ответа на звонок шлюз:

1. слушает линию через VAD (wake word не нужен);
2. отправляет `audio.start` (16 кГц) → PCM 640 Б → `audio.end`;
3. получает `assistant.reply` + `audio.url`, проигрывает ответ в трубку;
4. после `playback.stopped` при `listen: true` ждёт продолжения и снова шлёт
   `audio.start` с `follow_up: true`;
5. при `bridge_room` вместо голосового цикла делает `call.bridge` и льёт PCM
   в интерком-сессию.

## Камера в HA без HACS

Встроенная **Generic Camera**:

- Still Image URL: `http://<HA-хост>:8099/api/camera/snapshot.jpg?token=API_TOKEN`
- Stream Source: `http://<HA-хост>:8099/api/camera/stream.mjpg?token=API_TOKEN`
- Authentication: без авторизации.

После этого камера доступна как обычная сущность `camera.*`.

## Веб-интерфейс

После запуска приложения в сайдбаре HA появляется пункт **Athena GSM** (ingress).
В нём — видеопоток, статус модема, кнопки звонка/SMS/USSD и журнал событий.
При первом открытии введите `api_token` в кнопке «Токен API».

## Ограничения

- Поддерживаются архитектуры `amd64` и `aarch64`.
- Для реального модема нужен проброшенный `/dev/ttyUSB2`; проверьте
  `ls /dev/ttyUSB*` на хосте HA.
- Для камеры нужен `ffmpeg` (уже в образе) и `/dev/video0`.
- Приложение не содержит секретов: токены задаются в UI и хранятся Supervisor.
