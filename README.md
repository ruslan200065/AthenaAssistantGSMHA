# Athena SIM7600 Gateway

Клиентская прослойка между **ядром Athena** и **GSM-модемом SIM7600**.
Отдельный процесс на Linux (Raspberry Pi 5, мини-ПК): подключается к ядру по
`device-protocol/1`, принимает звонки/SMS, ведёт голосовой диалог и отдаёт
локальный REST/SSE API для Home Assistant.

```
┌──────────────┐   WebSocket (device-protocol/1)   ┌────────────────────┐
│ Athena Core  │◄─────────────────────────────────►│ sim7600-gateway    │
└──────────────┘   звонки · SMS · USSD · голос     │  AthenaClient      │
                                                   │  SIM7600 (AT/PCM)  │
┌──────────────┐   HTTP REST + SSE                 │  audio_bridge      │
│ Home         │◄─────────────────────────────────►└─────────┬──────────┘
│ Assistant    │   статус · SMS · события                     │ USB
└──────────────┘                                    ┌─────────▼──────────┐
                                                    │ SIM7600 + /dev/tty │
                                                    └────────────────────┘
```

## Возможности

- **Звонки**: входящие/исходящие, `ATD`, `ATA`, `AT+CHUP` (fallback `ATH`),
  DTMF, определение номера, локальная политика `allow`/`notify`/`reject`.
- **Разговор с Афиной по телефону**: после `ATA` шлюз слушает линию (VAD),
  отправляет `audio.start` + PCM 16 кГц + `audio.end`, проигрывает ответы
  `audio.url` (все части) в трубку и завершает ход по `audio.done`.
- **SMS**: GSM-7/UCS-2, склейка длинных сообщений, PDU-кодек без зависимостей.
- **USSD**: `AT+CUSD`, ответ уходит в ядро как `device.event/ussd.result`.
- **Мост на колонку**: только при явном `mode:"bridge"` / `bridge_room` —
  `call.bridge` и PCM в intercom-сессию Athena.
- **Home Assistant**: сенсоры модема, бинарные сенсоры, кнопки, notify-SMS,
  сервисы и события шины.
- **Надёжность**: watchdog AT, автоперезапуск модема, реконнект к ядру,
  идемпотентные `command.ack`.

## Быстрый старт (симулятор)

```bash
cd AthenaAssistantGSMHA
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m sim7600_gateway --mock --no-athena --port 8099
```

Дашборд: <http://127.0.0.1:8099>. Симулятор умеет `POST /api/dev/call`,
`/api/dev/sms`, `/api/dev/dtmf`, `/api/dev/hangup`.

## Установка на устройство

```bash
sudo ./deploy/install.sh
sudo nano /etc/config.toml
sudo systemctl restart sim7600-gateway
```

Docker: `cd deploy && docker compose up -d --build` (пробросьте `/dev/ttyUSB2`,
`/dev/ttyUSB4`, `/dev/snd` при необходимости).

## Подключение к Athena

1. В UI Athena создайте endpoint `kind = "gsm"` и выпустите токен `dev_...`.
2. Заполните конфиг:

```toml
[athena]
enabled = true
url = "ws://192.168.1.10:8000/api/device/ws"
token = "dev_ВАШ_ТОКЕН"
room_hint = "Прихожая"
```

3. Шлюз отправит `hello` с возможностями
   `["calls","sms","dtmf","audio_in","audio_out"]` и heartbeat каждые 20 с.

Подробный маппинг кадров — [docs/ATHENA_PROTOCOL.md](docs/ATHENA_PROTOCOL.md).

## Home Assistant (add-on)

1. «Настройки → Дополнения → Магазин → ⋮ → Репозитории» → добавьте
   `https://github.com/ruslan200065/AthenaAssistantGSMHA`.
2. Установите **Athena SIM7600 Gateway**.
3. Настройки: `athena_url`, `athena_token`, `api_token`, `modem_port`,
   `pcm_port` (`/dev/ttyUSB4`), `audio_enabled`, `playback_volume`.
4. Запустите; панель откроется через ingress, API-токен также в
   `/data/api_token.txt`.

Для сущностей HA установите HACS-компонент `custom_components/athena_sim7600`
из того же репозитория: сенсоры, бинарные сенсоры, кнопки, notify-SMS и сервисы.

## Конфигурация

Секции: `[bridge]`, `[athena]`, `[modem]`, `[audio_bridge]`.

| Параметр | Смысл |
|---|---|
| `bridge.api_token` | Bearer-токен локального API |
| `athena.url` / `athena.token` | WebSocket ядра и токен `dev_...` |
| `modem.port` | AT-порт, обычно `/dev/ttyUSB2` |
| `modem.unknown_callers` | `allow` / `notify` / `reject` |
| `modem.allowed_numbers` | «Свои» номера для автоответа/моста |
| `audio_bridge.enabled` | Голосовой цикл и intercom |
| `audio_bridge.pcm_port` | Сырой PCM-порт SIM7600, обычно `/dev/ttyUSB4` |
| `audio_bridge.playback_volume` | Громкость ответов в трубку, 0–200 (50) |

ALSA (`arecord`/`aplay`) используется как fallback, если PCM-порт недоступен.

## REST API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/health` | Публичный health-check |
| GET | `/api/status` | Статус шлюза |
| POST | `/api/sms` | `{number, text}` |
| POST | `/api/ussd` | `{code}` → ответ сети |
| POST | `/api/dial` | `{number}` |
| POST | `/api/calls/answer` / `hangup` / `dtmf` | управление звонком |
| POST | `/api/calls/bridge` | `{room}` → `call.bridge` |
| GET | `/api/events` | SSE-поток событий |
| POST | `/api/modem/reset` | Перезапуск модуля |

Авторизация: `Authorization: Bearer <api_token>` или `X-Api-Token`;
`?token=` допускается только для `/api/events*`.

## Проверка

```bash
python scripts/athena_smoke.py
python -m sim7600_gateway.tools.probe --port /dev/ttyUSB2
```

## Документация

- [Архитектура](docs/ARCHITECTURE.md)
- [Протокол Athena](docs/ATHENA_PROTOCOL.md)
- [Модем и AT](docs/MODEM.md)
- [Голосовой звонок](docs/CALL_VOICE.md)
- [Home Assistant](docs/HOME_ASSISTANT.md)
- [REST API](docs/API.md)
- [Диагностика](docs/TROUBLESHOOTING.md)

## Приватность

В логи не попадают полные номера и тексты SMS. API защищён токеном. Записи
разговоров нет; PCM живёт только во время активного звонка/интеркома.
