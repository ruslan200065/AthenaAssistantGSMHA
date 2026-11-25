# Диагностика

## Быстрый чек-лист

```bash
# 1. Процесс жив?
systemctl status sim7600-gateway
journalctl -u sim7600-gateway -n 100 --no-pager

# 2. HTTP отвечает?
curl http://127.0.0.1:8099/api/health

# 3. Модем отвечает по AT?
python -m sim7600_gateway.tools.probe --modem-only

# 4. Камера видна?
python -m sim7600_gateway.tools.probe --camera-only

# 5. Полный статус (нужен токен)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8099/api/status | python -m json.tool
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8099/api/diagnostics | python -m json.tool
```

## Модем

### Порт не открывается / Permission denied

```bash
ls -l /dev/ttyUSB* /dev/sim7600*
sudo usermod -aG dialout,video,audio sim7600
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Проверьте, что это AT-порт: `python -m sim7600_gateway.tools.probe --port /dev/ttyUSB2`.
Если `ttyUSB2` молчит, попробуйте `ttyUSB1`/`ttyUSB3` (зависит от прошивки и
порядка интерфейсов).

### `AT watchdog: несколько таймаутов подряд`

- Питание: SIM7600 любит просадки при передаче; используйте БП ≥ 2 А.
- Кабель: некоторые USB-кабели только для зарядки.
- `dmesg` на предмет `disconnect`, `over-current`.
- Пробуйте `reset_method = "gpio"` с пином сброса HAT.
- Убедитесь, что порт не занят `ModemManager`: он может дёргать AT-порт.

```bash
sudo systemctl stop ModemManager
sudo systemctl disable ModemManager
```

### Нет регистрации в сети

- `AT+CPIN?` → `READY`; иначе SIM не читается.
- `AT+CSQ` → `99,99` означает нет сигнала (антенна/место).
- `AT+COPS?` → оператор; `AT+CREG?` → 1 или 5.
- Проверьте баланс/SIM в телефоне.
- `AT+CFUN=1` / перезагрузка модема через `/api/modem/reset`.

### SMS не отправляются

- `+CMS ERROR: 304` — проблема PDU/SIM; смотрите `at_log` в диагностике.
- Длинный текст: проверьте `parts` в ответе API.
- `AT+CMGF?` должен быть `0` (PDU).
- Память SIM переполнена: `AT+CPMS?`, очистите `AT+CMGD=1,4`.

### Входящие SMS не приходят

- `AT+CNMI?` должен быть `2,1,0,0,0`.
- Проверьте `+CMTI` в AT-логе.
- Если SMS приходят как `+CMT` (другая прошивка), шлюз тоже их разбирает.

### USSD без ответа

- Оператор может не поддерживать USSD при активном звонке.
- `+CME ERROR: 3` — операция запрещена; завершите звонок.
- Проверьте код: только `0-9*#+`, до 32 символов.
- Таймаут 30 с; некоторые операторы отвечают несколькими `+CUSD` — шлюз берёт
  первый.

### Звонок не определяется

- `AT+CLIP=1` в init.
- Номер может быть скрыт оператором; шлюз пробует `AT+CLCC`, при неудаче не
  отправляет `call.incoming`.
- Проверьте `caller` в `/api/status`.

## Athena

### `auth` / `not_found`

Токен должен начинаться с `dev_`, endpoint должен иметь `kind="gsm"`.
Проверьте `athena.url` (порт и путь `/api/device/ws`).

### `replaced by a newer connection` (close 4000)

Кто-то ещё подключился с этим же токеном (второй шлюз/копия). Шлюз не будет
переподключаться, пока не перезапустится. Остановите дубликат.

### `revoked`

Токен отозван в ядре. Выпустите новый и обновите конфиг; шлюз повторит попытку
через 10 минут (или перезапустите сервис).

### События не доходят до ядра

- `/api/status → athena.connected` должно быть `true`.
- Шлюз не переотправляет события после реконнекта (по протоколу). Проверьте
  `events.recent` — события были, но соединение упало.
- `call.incoming` отправляется до ответа; если ядро не успело — проверьте
  задержку сети.

## Камера

### `ffmpeg not found`

```bash
sudo apt install ffmpeg v4l-utils
# или пропишите полный путь: camera.ffmpeg = "/usr/bin/ffmpeg"
```

### `camera frame is not available`

- `v4l2-ctl --device /dev/video0 --list-formats-ext` — есть ли форматы.
- Камера занята другим процессом (Motion, ZoneMinder, браузер).
- Неверный `input_format`: попробуйте `mjpg`, затем `yuyv`.
- Уменьшите разрешение/fps: некоторые камеры не держат 1080p MJPEG.
- Проверьте `dmesg` на USB-ошибки; попробуйте другой порт USB 3.0.

### Чёрный кадр / артефакты

- `-q:v 4` уже в pipeline; уменьшите разрешение.
- Камера отдаёт H264 — оставьте `input_format="auto"` или `"h264"`.
- Для очень дешёвых камер MJPG может быть нестабилен: `input_format="yuyv"`.

### HA не показывает поток

- Снимки работают? Значит камера жива, проблема в потоке.
- HA stream может не открыть MJPEG; используйте iframe или picture-entity.
- Проверьте токен в `stream_url` (он в атрибутах сущности).

### Движение срабатывает постоянно

- Ночной шум/автоэкспозиция: поднимите `motion_threshold` до 0.06–0.1.
- Увеличьте `motion_cooldown`.
- В mock-режиме `testsrc2` движется всегда — это ожидаемо.

## Home Assistant

### «Не удалось подключиться»

- URL должен быть доступен из контейнера HA (не `127.0.0.1`, если HA в Docker).
- Проверьте токен: `curl -H "Authorization: Bearer ..." http://IP:8099/api/status`.
- Для HTTPS с самоподписанным сертификатом выключите «Проверять SSL».

### Сущности «недоступны»

Интеграция опрашивает `/api/status` каждые 15 с; если шлюз перезапускается,
подождите. SSE-события приходят отдельным соединением — проверьте логи HA
(`athena_sim7600`).

### События не появляются в автоматизациях

Слушайте `athena_sim7600_event` (общий) или конкретные события. В
«Инструменты разработчика → События» подпишитесь и позвоните на шлюз.

## Аудио и тишина

- REST `/api/calls/answer` делает только `ATA` — тишина by design. Голосовой ответ
  инициирует Athena командой `call {action:"answer", mode:"assistant"|"bridge"}`.
- Голос требует `[audio_bridge].enabled=true`, `arecord`/`aplay` и USB-аудио модема
  (или `modem.mock`). Иначе команда вернёт failed
  `voice audio is unavailable...`; причина видна в `/api/status → voice_call.last_error`.
- `bridge_call`/`mode=bridge` требует аудио и Athena. При ошибке bridge звонок
  остаётся активным с fallback на assistant voice, либо завершается, если уже сброшен.
- Нет `assistant.reply` за 45 с → диалог закрывается; follow-up ждёт 3–8 с
  (минимум 3, по умолчанию 8).
- Проверьте `arecord -l`, `aplay -l`; устройства в `[audio_bridge]` должны
  существовать (`auto` ищет карту с «SIM7600»).
- `AT+CPCMREG=1` может не поддерживаться прошивкой — смотрите warning в логах.
- Эхо/тишина: проверьте маршрутизацию ALSA (`alsamixer`) и уровень микрофона.
- Аудио включено в HA add-on (`audio_enabled: true`); в `config.example.toml`
  по умолчанию выключено — включайте после проверки звонков.

## Логи

```toml
[bridge]
log_level = "DEBUG"
```

DEBUG показывает AT-обмен (без номеров и PDU), кадры ffmpeg и SSE-события.
Не оставляйте DEBUG на постоянной работе — растёт журнал.

## Сбор диагностики для issue

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8099/api/diagnostics > diag.json
journalctl -u sim7600-gateway -n 300 --no-pager > gateway.log
python -m sim7600_gateway.tools.probe --modem-only > probe.txt 2>&1
```

Приложите `diag.json`, `gateway.log`, `probe.txt`, модель модема/камеры и
версию ОС. Токены и номера лучше заменить на `***` перед публикацией.
