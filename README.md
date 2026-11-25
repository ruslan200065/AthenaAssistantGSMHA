# Athena SIM7600 Gateway

Клиентская прослойка между **ядром Athena**, **GSM-модемом SIM7600** и **USB-камерой**.
Отдельный процесс на Linux (Raspberry Pi 5, мини-ПК), который подключается к ядру по
`device-protocol/1` и одновременно отдаёт локальный REST/SSE API для Home Assistant.

```
┌──────────────┐   WebSocket (device-protocol/1)   ┌─────────────────────┐
│ Athena Core  │◄─────────────────────────────────►│  sim7600-gateway    │
│  FastAPI     │   звонки · SMS · USSD · события   │  ┌───────────────┐  │
└──────────────┘                                   │  │ AthenaClient  │  │
                                                   │  ├───────────────┤  │
┌──────────────┐   HTTP REST + SSE (localhost)     │  │ SIM7600 (AT)  │  │
│ Home         │◄─────────────────────────────────►│  ├───────────────┤  │
│ Assistant    │   статус · SMS · камера · события │  │ Camera (V4L2) │  │
└──────────────┘                                   │  └───────────────┘  │
                                                   └─────────┬───────────┘
                                                             │ USB
                                                   ┌─────────▼───────────┐
                                                   │ SIM7600 + /dev/video│
                                                   └─────────────────────┘
```

## Возможности

- **Звонки**: входящие и исходящие, `ATD`, `ATA`, `ATH`, `+CLIP`, DTMF, определение
  номера, локальная политика (`allow` / `notify` / `reject`), автоответ.
- **SMS**: отправка и приём, GSM-7 и UCS-2, склейка длинных сообщений (UDH), полный
  PDU-кодек без внешних зависимостей. Номера и тексты не попадают в логи.
- **USSD**: запрос баланса и сервисных меню, ответ уходит в ядро событием
  `device.event/ussd.result`.
- **Мост в Athena**: после ответа на звонок шлёт `call.bridge`; при `intercom.active`
  может поднять PCM-мост 16 кГц через USB-аудио модема.
- **Камера**: MJPEG-поток через ffmpeg, снимки, короткие клипы, детектор движения,
  снимок при входящем звонке, интеграция в Home Assistant как `camera`.
- **Home Assistant**: HACS-интеграция с сенсорами, бинарными сенсорами, кнопками,
  камерой, notify-сущностью, сервисами и событиями шины.
- **Режим симулятора**: полноценный демо-стенд без железа (`--mock`).
- **Надёжность**: watchdog AT-канала, автоперезапуск модема, реконнект к ядру с
  экспоненциальной задержкой, идемпотентные `command.ack`.

## Быстрый старт без железа

```bash
cd sim7600-gateway
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
python -m sim7600_gateway --mock --no-athena --port 8099
```

Откройте <http://127.0.0.1:8099> — дашборд с симулированным модемом и камерой.
Токен по умолчанию пустой; для демонстрации API можно не задавать. Симулятор умеет:

```bash
# входящий звонок
curl -X POST http://127.0.0.1:8099/api/dev/call -H 'Content-Type: application/json' \
     -d '{"number":"+79991234567"}'
# входящая SMS
curl -X POST http://127.0.0.1:8099/api/dev/sms -H 'Content-Type: application/json' \
     -d '{"number":"+79995554433","text":"Привет от симулятора"}'
# завершить звонок на стороне абонента
curl -X POST http://127.0.0.1:8099/api/dev/hangup
```

## Установка на устройство

### Linux (systemd)

```bash
sudo ./deploy/install.sh
sudo nano /etc/sim7600-gateway/config.toml   # URL/токен Athena, API-токен
sudo systemctl restart sim7600-gateway
```

Скрипт создаёт пользователя `sim7600`, venv в `/opt/sim7600-gateway`, udev-правила
для SIMCom `1E0E:9001`, конфиг в `/etc/sim7600-gateway` и state в
`/var/lib/sim7600-gateway`.

### Docker

```bash
cd deploy
cp ../config.example.toml config.toml
# отредактируйте config.toml и пробросьте свои устройства в docker-compose.yml
docker compose up -d --build
```

### Windows (разработка)

```powershell
pip install -r requirements.txt
python -m sim7600_gateway --config config.toml
```

Для реального модема укажите `MODEM_PORT=COM5` (AT-порт). Камера на Windows
поддерживается только в mock-режиме: реальный драйвер — V4L2/Linux.

## Подключение к Athena

1. В UI Athena создайте endpoint с `kind = "gsm"` (комната — по желанию).
2. Выпустите токен устройства (`dev_...`) и впишите его в конфиг:

```toml
[athena]
enabled = true
url = "ws://192.168.1.10:8000/api/device/ws"
token = "dev_ВАШ_ТОКЕН"
room_hint = "Прихожая"
```

3. Шлюз сам отправит `hello` с возможностями
   `["calls","sms","dtmf","audio_in","audio_out","camera"]`.
4. Проверьте в ядре: устройство онлайн, heartbeat каждые 20 с.

Ядро шлёт команды `call`, `sms`, `ussd`, `reboot`, `config`; всё остальное шлюз
корректно отклоняет (`failed`), не ломая сессию. Исходящие события: `call.incoming`,
`call.state`, `sms.incoming`, `dtmf`, `device.event`, `call.bridge`.

Подробный маппинг — [docs/ATHENA_PROTOCOL.md](docs/ATHENA_PROTOCOL.md).

## Home Assistant

```bash
cp -r custom_components/athena_sim7600 /config/custom_components/
```

Перезапустите HA → «Настройки → Устройства и службы → Добавить интеграцию →
Athena SIM7600 GSM». Укажите URL шлюза (`http://192.168.1.50:8099`) и
`bridge.api_token`.

Репозиторий можно добавить в HACS как custom repository (категория Integration):
`custom_components/athena_sim7600` уже лежит в корне.

> Для проверки без модема запустите шлюз на любой машине в сети:
> `python -m sim7600_gateway --mock --no-athena --port 8099`.
> В `config.toml` задайте `bridge.api_token` и используйте этот же токен в HA.

Появятся:

- сенсоры: состояние модема, сигнал (dBm/%), оператор, сеть, регистрация, SIM,
  время работы, счётчики SMS, последняя SMS, камера, Athena;
- бинарные сенсоры: онлайн, регистрация, звонок, входящий звонок, движение,
  камера, Athena;
- кнопки: снимок, ответить, сбросить, перезапустить модем;
- камера (снимки + MJPEG-поток);
- notify-сущность `notify.athena_gsm_sms`;
- сервисы: `send_sms`, `send_ussd`, `dial`, `answer_call`, `hangup_call`,
  `reset_modem`, `take_snapshot`, `record_clip`, `bridge_call`.

Пример автоматизации «SMS при движении»:

```yaml
alias: GSM — SMS при движении
trigger:
  - platform: state
    entity_id: binary_sensor.athena_gsm_dvizhenie
    to: "on"
action:
  - service: athena_sim7600.send_sms
    data:
      number: "+79991234567"
      text: "Движение у камеры"
```

Больше примеров — [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md).

## Конфигурация

Все параметры — в `config.example.toml`; переменные окружения перекрывают TOML.
Секции: `[bridge]`, `[athena]`, `[modem]`, `[audio_bridge]`, `[camera]`.

Ключевые:

| Параметр | Смысл |
|---|---|
| `bridge.api_token` | Bearer-токен локального API (обязательно задайте) |
| `athena.url` / `athena.token` | WebSocket ядра и токен `dev_...` |
| `modem.port` | AT-порт, обычно `/dev/ttyUSB2` или `COM5` |
| `modem.unknown_callers` | `allow` / `notify` / `reject` — локальная политика |
| `modem.allowed_numbers` | «Свои» номера для автоответа и моста |
| `modem.bridge_room` | Комната, куда соединять звонок через `call.bridge` |
| `camera.device` | `/dev/video0` |
| `camera.snapshot_on_ring` | Снимок при входящем звонке |

## REST API

Полный список — [docs/API.md](docs/API.md). Кратко:

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/health` | Публичный health-check |
| GET | `/api/status` | Полный статус шлюза |
| POST | `/api/sms` | `{number, text}` |
| POST | `/api/ussd` | `{code}` → ответ сети |
| POST | `/api/dial` | `{number}` |
| POST | `/api/calls/answer` / `hangup` / `dtmf` | управление звонком |
| POST | `/api/calls/bridge` | `{room}` → `call.bridge` в ядро |
| GET | `/api/camera/snapshot.jpg` | JPEG-кадр |
| GET | `/api/camera/stream.mjpg` | MJPEG-поток |
| GET | `/api/events` | SSE-поток событий |
| GET | `/api/events/recent` | История событий |
| POST | `/api/modem/reset` | Перезапуск модуля |

Авторизация: `Authorization: Bearer <bridge.api_token>` или `X-Api-Token`.
Для `<img>`/`EventSource` допускается `?token=...`. Камеру можно открыть без
токена флагом `bridge.allow_anonymous_camera = true`.

## Тесты

```bash
cd sim7600-gateway
python -m pytest -q
```

47 тестов: PDU-кодек (эталонные векторы, UCS-2, склейка), AT-канал, симулятор
модема, WebSocket-клиент ядра против локального сервера, REST API, конфиг, камера,
полный сценарий bridge с фейковым ядром.

## Структура

```
AthenaAssistantGSMHA/
├── sim7600_gateway/
│   ├── athena/          # клиент device-protocol/1, кадры
│   ├── modem/           # AT-канал, PDU, SIM7600, симулятор
│   ├── camera/          # ffmpeg/V4L2, MJPEG, движение
│   ├── app.py           # FastAPI + SSE
│   ├── bridge.py        # оркестрация и политики
│   ├── audio_bridge.py  # PCM-мост (опционально)
│   └── dashboard.py     # локальный UI
├── custom_components/athena_sim7600/
├── deploy/              # systemd, Docker, udev
├── docs/
└── tests/
```

## Приватность и безопасность

- В логи не попадают полные номера и тексты SMS (маскирование).
- API защищён Bearer-токеном; камера по умолчанию тоже.
- Нет записи разговоров. Аудиомост передаёт PCM только в рамках активной
  intercom-сессии Athena.
- Рекомендуется держать шлюз в домашней сети/VPN и не публиковать порт 8099 наружу.

## Документация

- [Архитектура](docs/ARCHITECTURE.md)
- [Протокол Athena](docs/ATHENA_PROTOCOL.md)
- [Модем и AT-команды](docs/MODEM.md)
- [Камера](docs/CAMERA.md)
- [Home Assistant](docs/HOME_ASSISTANT.md)
- [REST API](docs/API.md)
- [Диагностика](docs/TROUBLESHOOTING.md)

