# Камера

## Реальный источник (Linux / V4L2)

Шлюз не работает с камерой напрямую через ioctl: он запускает `ffmpeg`, который
читает `/dev/video0` и отдаёт непрерывный MJPEG-поток в stdout. Это даёт
предсказуемый JPEG-поток для MJPEG-эндпоинта, снимков и HA.

```bash
v4l2-ctl --device /dev/video0 --list-formats-ext
```

Поддерживаемые форматы входа: `MJPG`, `YUYV`, `NV12`, `H264`. При
`input_format="auto"` выбирается первый доступный в порядке
`MJPG → H264 → YUYV → NV12`. Размер и fps берутся из конфига; драйвер сам
подберёт ближайший поддерживаемый режим.

Требования:

- Linux с V4L2, пользователь в группе `video`;
- `ffmpeg` в `PATH` (или `camera.ffmpeg`);
- `v4l-utils` желателен для автоопределения форматов (`camera.v4l2ctl`).

Проверка:

```bash
python -m sim7600_gateway.tools.probe --camera-only
```

## Pipeline

```
/dev/video0 ──ffmpeg──► stdout (JPEG SOI...EOI) ──► FrameHub ──► MJPEG-подписчики
                                                       ├──► snapshot.jpg
                                                       ├──► motion detector (Pillow)
                                                       └──► clip recorder (отдельный ffmpeg)
```

`FfmpegCamera._read_frames` режет байтовый поток по маркерам `FFD8`/`FFD9`,
поэтому кадры не «плывут» даже при частичных чтениях. При падении ffmpeg
перезапуск идёт с задержкой 2→3→4.5… до 15 с.

## Mock-режим

При `camera.mock = true` источник — `lavfi:testsrc2`:

```bash
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -c:v mjpeg -q:v 4 -f mjpeg -
```

Это позволяет проверить весь путь (MJPEG, снимки, запись, движение, HA) без
камеры. На Windows доступен только mock-режим.

## Эндпоинты

| Метод | Путь | Результат |
|---|---|---|
| GET | `/api/camera/info` | формат, разрешение, fps, frames, motion, последние снимки |
| GET | `/api/camera/snapshot.jpg` | текущий JPEG (503, если кадра нет) |
| GET | `/api/camera/snapshot.jpg?save=true` | плюс сохранить файл, заголовок `X-Snapshot-File` |
| GET | `/api/camera/stream.mjpg` | `multipart/x-mixed-replace; boundary=athena-frame` |
| POST | `/api/camera/record` | `{seconds: 12}` → mp4 в `data/clips` |
| GET | `/api/camera/clips/{name}` | отдача mp4 |
| GET | `/api/camera/snapshots` | последние снимки |

Для `<img>` и Home Assistant камера принимает `?token=<bridge.api_token>`.
Альтернатива — `bridge.allow_anonymous_camera = true` (только камера, без
управления).

## Снимки

- `snapshot_on_ring = true` — при входящем звонке в фоне сохраняется
  `data/snapshots/ring_YYYYmmdd_HHMMSS.jpg`, событие `camera.snapshot` уходит в
  SSE и (как `device.event`) в ядро.
- `clip_on_ring = true` — параллельно пишется клип `clip_...mp4`
  длительностью `clip_seconds`.
- Кнопка «Снимок» в дашборде, сервис HA `take_snapshot`, REST с `save=true`.

Снимок не блокирует обработку звонка: `latest_frame()` ждёт кадр до 3 с, при
отсутствии возвращает `None`, звонок продолжается.

## Детектор движения

Каждый кадр уменьшается до 64×36 в градациях серого (Pillow), сравнивается с
предыдущим; доля пикселей с разницей > 25 считается изменением.

- порог: `motion_threshold` (по умолчанию 0.035 = 3.5 %);
- пауза между событиями: `motion_cooldown` (30 с);
- событие: `camera.motion {changed, threshold, at}`;
- в HA: `binary_sensor.*_dvizhenie` (60 секунд после события).

Детектор работает в отдельном потоке (`asyncio.to_thread`), поэтому не тормозит
очередь кадров. В mock-режиме `testsrc2` движется постоянно — это нормально для
демонстрации; для продакшена используйте реальную камеру.

## Производительность

- 1280×720 @ 15 fps, MJPEG q4 — разумный баланс для Raspberry Pi 5.
- Для 4K уменьшайте fps и не включайте motion detection без необходимости.
- Один ffmpeg-процесс на поток; запись клипа — второй процесс (кратковременно).
- Не открывайте `stream.mjpg` в нескольких вкладках без надобности: каждый
  подписчик получает копию кадра из памяти, но сеть расходуется.

## Home Assistant

Интеграция создаёт `camera.athena_gsm_kamera`:

- `async_camera_image` — JPEG через `/api/camera/snapshot.jpg`;
- `stream_source` — MJPEG URL с токеном (HA stream/HLS-плеер);
- атрибуты: устройство, разрешение, fps, frames, motion_detection.

Если HA не может открыть поток, снимки всё равно работают. Для гарантированно
живого видео в Lovelace можно использовать `picture-entity` с
`camera_image: /api/camera/stream.mjpg?token=...` или iframe на дашборд шлюза.
