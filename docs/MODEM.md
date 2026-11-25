# Модем SIM7600

## Подключение

SIM7600 (USB-донгл или HAT) обычно поднимает несколько интерфейсов:

```bash
lsusb                      # 1E0E:9001 SIMCom Wireless
dmesg | grep -i ttyUSB
ls -l /dev/ttyUSB*
ls -l /dev/serial/by-id/
```

| Порт | Обычно | Назначение |
|---|---|---|
| `ttyUSB0` | DIAG | диагностика Qualcomm |
| `ttyUSB1` | NMEA | GPS |
| `ttyUSB2` | **AT** | команды модема |
| `ttyUSB3` | MODEM | PPP/data |

Берите порт, который отвечает на `AT` (проверка: `python -m sim7600_gateway.tools.probe --modem-only`).
Права: пользователь должен быть в группе `dialout`.

Стабильные имена даёт `deploy/99-sim7600.rules`:

```udev
SUBSYSTEM=="tty", ATTRS{idVendor}=="1e0e", ATTRS{idProduct}=="9001", SYMLINK+="sim7600%n", GROUP="dialout", MODE="0660"
```

Тогда AT-порт — `/dev/sim76002` (номер зависит от порядка интерфейсов).

## Инициализация

Порядок из `[modem].init_sequence`:

```
ATE0             # без эха
AT+CMEE=2        # подробные ошибки
AT+CMGF=0        # PDU-режим SMS
AT+CNMI=2,1,0,0,0  # +CMTI при новой SMS
AT+CLIP=1        # определение номера
AT+CRC=1         # +CRING для типов звонка
AT+DDET=1        # DTMF
AT+CPMS="ME","ME","ME"
```

Если `ATE0` не отвечает — модем не готов, шлюз уходит в retry. Остальные ошибки
не фатальны: шлюз логирует и продолжает, чтобы диагностика работала даже с
частично рабочей SIM.

Идентификация: `AT+CGMI`, `AT+GMM`, `AT+CGMR`, `AT+CGSN` (IMEI), `AT+CCID`,
`AT+CNUM`.

Статус (каждые 30 с): `AT+CPIN?`, `AT+CSQ`, `AT+CREG?` → `AT+CGREG?`,
`AT+COPS?` (раз в 5 минут), `AT+CPSI?`.

## URC и их обработка

| URC | Действие |
|---|---|
| `RING`, `+CRING` | Начать звонок, подождать `+CLIP` 0.6 с |
| `+CLIP: "+79...",145` | Определить номер |
| `NO CARRIER`, `+CEND` | Завершить/пропустить звонок |
| `VOICE CALL: BEGIN` | Линия активна → `call.state active` |
| `VOICE CALL: END: 000123` | Завершить, duration из URC |
| `+CMTI: "ME",7` | Прочитать SMS `AT+CMGR=7` |
| `+CMT: ...` + PDU | Прямая доставка SMS (если CNMI настроен на 2) |
| `+CUSD: 0,"...",15` | Ответ USSD |
| `+DTMF: 5`, `+RXDTMF: 5` | Событие `dtmf` |
| `+CREG`/`+CGREG`/`+CEREG` | Обновить регистрацию |
| `RDY`, `*ATREADY`, `SMS DONE` | Игнорируются |

AT-канал различает «ответ на команду» и URC: пока команда в полёте, строки
складываются в её ответ, а однозначные URC (`RING`, `+CMTI`, …) уходят в
обработчик. Двусмысленные (`+CREG`, `+CLCC`) считаются ответом, если есть
ожидающая команда.

## SMS

Режим PDU (`AT+CMGF=0`) выбран сознательно: он одинаково работает с кириллицей
и склейкой.

- GSM-7: до 160 символов, `€ { } [ ] ~ | ^ \` считаются за два.
- UCS-2: до 70 символов (кириллица, эмодзи).
- Длиннее — UDH-склейка (153/67 символов на часть, до 8 частей).
- Приём: `parse_deliver` поддерживает GSM-7, UCS-2, UDH, alphanumeric-отправителя
  и SCTS.
- Отправка: `AT+CMGS=<длина TPDU>`, ждём `>`, пишем PDU + `0x1A`, ждём
  `+CMGS: <mr>` и `OK` (таймаут 60 с).
- После успешного чтения SMS удаляется `AT+CMGD=<index>`.

Проверка кодека:

```bash
python -m pytest tests/test_pdu.py -q
```

## USSD

```
AT+CUSD=1,"*100#",15
OK
+CUSD: 0,"0411...",72     # UCS-2 hex
```

Шлюз декодирует UCS-2 (DCS 72), 8-бит (DCS 68) и обычный текст. Ответ
возвращается и в REST, и в ядро как `device.event/ussd.result`. Таймаут
ответа — 30 с. Одновременно допускается один USSD-запрос.

## Звонки и DTMF

- Исходящий: `ATD+79991234567;` (голосовой вызов, точка с запятой обязательна).
  `OK` означает «модем принял», а не «абонент ответил».
- Входящий: `RING` → `ATA`.
- Сброс: `AT+CHUP` (основная команда), `ATH` — совместимый fallback.
- DTMF: `AT+VTS=<digit>` по одному символу.
- Удержание для моста: `AT+CHLD=2` (только при `hold_before_bridge = true`).
- Аудио: `AT+CPCMREG=1` включает маршрут PCM в USB-аудио, `AT+CPCMREG=0` выключает.
  Маршрут используется и для assistant voice (`voice_call.py`), и для intercom
  bridge (`audio_bridge.py`): 16 кГц mono s16le, кадры 640 Б / 20 мс. Подробнее —
  [CALL_VOICE.md](CALL_VOICE.md).

Если номер не определился, шлюз пробует `AT+CLCC`; при неудаче звонок не
уходит в ядро. Политика `reject` отклоняет такой звонок через `ATH`.

## Watchdog

- 3 таймаута AT подряд → `recover()`.
- `reset_method="at"`: `AT+CFUN=1,1`, пауза 3 с, переоткрытие порта.
- `reset_method="gpio"`: импульс на `reset_gpio` через `/sys/class/gpio`.
- `reset_method="none"`: только переоткрытие порта.
- До 5 попыток с задержкой 5–30 с; затем состояние `off` и повтор из
  супервизора каждые 10 с.

## Частые AT-ошибки

| Ответ | Причина |
|---|---|
| `+CME ERROR: 10` | SIM не вставлена / не инициализирована |
| `+CME ERROR: 13` | SIM failure |
| `+CMS ERROR: 304` | неверный PDU или нет денег/сервиса |
| `+CMS ERROR: 500` | неизвестная ошибка сети |
| `+CME ERROR: 3` | операция не разрешена (например, USSD при занятой линии) |
| `BUSY` | уже идёт звонок |
| `NO CARRIER` | линия разорвана / вызов отклонён |

Полезные команды диагностики:

```bash
python -m sim7600_gateway.tools.probe --modem-only
curl -H "Authorization: Bearer TOKEN" http://127.0.0.1:8099/api/diagnostics
```
