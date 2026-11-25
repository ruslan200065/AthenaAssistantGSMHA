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
5. Добавьте интеграцию **Athena SIM7600 GSM** (HACS или вручную), URL
   `http://<HA-хост>:8099`, токен = `api_token`.

Приложение работает с `host_network: true`, поэтому порт 8099 доступен напрямую,
а camera/GSM устройства проброшены через `full_access`, `uart`, `usb`, `video`.

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
