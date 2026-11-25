# Home Assistant

> Если шлюз установлен как **приложение HA (add-on)**, начните с
> `athena_sim7600_gateway/README.md`: камера подключается встроенной
> **Generic Camera**, HACS не нужен. Ниже — опциональная интеграция.

## Установка

### Вручную

```bash
cp -r custom_components/athena_sim7600 /config/custom_components/
```

Перезапустите Home Assistant.

### HACS (custom repository)

1. HACS → «Интеграции» → ⋮ → «Пользовательские репозитории».
2. URL: этот репозиторий (интеграция в корне).
3. Категория «Integration» → установить → перезапустить HA.

### Настройка

«Настройки → Устройства и службы → Добавить интеграцию → Athena SIM7600 GSM».

| Поле | Значение |
|---|---|
| URL | `http://192.168.1.50:8099` (адрес шлюза) |
| API-токен | `bridge.api_token` из конфига шлюза |
| Проверять SSL | выключить для самоподписанного HTTPS |

Интеграция проверяет `/api/status` и создаёт устройство «Athena GSM».

## Сущности

| Платформа | Пример | Описание |
|---|---|---|
| sensor | `sensor.athena_gsm_sostoyanie_modema` | `idle`, `ringing`, `active`, … |
| sensor | `sensor.athena_gsm_uroven_signala` | dBm |
| sensor | `sensor.athena_gsm_signal` | % |
| sensor | `sensor.athena_gsm_operator` | имя оператора |
| sensor | `sensor.athena_gsm_set` | LTE/UMTS/GSM |
| sensor | `sensor.athena_gsm_registratsiya` | `registered_home` и т.п. |
| sensor | `sensor.athena_gsm_sim` | `READY` |
| sensor | `sensor.athena_gsm_vremya_raboty` | секунды |
| sensor | `sensor.athena_gsm_sms_otpravleno` / `..._polucheno` | счётчики |
| sensor | `sensor.athena_gsm_poslednyaya_sms` | текст (до 250 символов) + атрибуты |
| sensor | `sensor.athena_gsm_kamera` | `онлайн` / `нет кадра` |
| sensor | `sensor.athena_gsm_athena` | связь с ядром |
| binary_sensor | `..._shlyuz_onlayn` | шлюз отвечает |
| binary_sensor | `..._zaregistrirovan_v_seti` | CREG |
| binary_sensor | `..._idet_zvonok` | активный звонок |
| binary_sensor | `..._vkhodyashchiy_zvonok` | ringing, атрибут `caller` |
| binary_sensor | `..._dvizhenie` | движение за последние 60 с |
| binary_sensor | `..._kamera_onlayn` | есть кадры |
| binary_sensor | `..._athena_podklyuchena` | WS к ядру |
| camera | `camera.athena_gsm_kamera` | снимки + MJPEG |
| button | `..._snimok_s_kamery`, `..._otvetit_na_zvonok`, `..._sbroson`, `..._perezapustit_modem` | действия |
| notify | `notify.athena_gsm_sms` | отправка SMS |

Некоторые сенсоры (IMEI, ICCID, модель, прошивка, номер SIM) отключены по
умолчанию — включите их в реестре сущностей.

## Сервисы

```yaml
# SMS
service: athena_sim7600.send_sms
data:
  number: "+79991234567"
  text: "Привет из Home Assistant"

# USSD (возвращает ответ)
service: athena_sim7600.send_ussd
data:
  code: "*100#"
response_variable: ussd

# Звонок
service: athena_sim7600.dial
data:
  number: "+79991234567"

# Управление звонком
service: athena_sim7600.answer_call
service: athena_sim7600.hangup_call

# Камера
service: athena_sim7600.take_snapshot
service: athena_sim7600.record_clip
data:
  seconds: 20

# Мост на колонку Athena (после входящего звонка)
service: athena_sim7600.bridge_call
data:
  room: "Гостиная"

# Диагностика
service: athena_sim7600.reset_modem
```

## События шины

| Событие | Данные |
|---|---|
| `athena_sim7600_event` | любой кадр SSE: `{seq, type, data, at}` |
| `athena_sim7600_sms_received` | `{number, text, at}` |
| `athena_sim7600_incoming_call` | `{number, at}` |
| `athena_sim7600_call_state` | `{state, number, duration_sec}` |
| `athena_sim7600_motion` | `{changed, threshold, at}` |

Пример автоматизации на SMS:

```yaml
alias: SMS → уведомление
trigger:
  - platform: event
    event_type: athena_sim7600_sms_received
action:
  - service: notify.mobile_app_phone
    data:
      title: "SMS от {{ trigger.event.data.number }}"
      message: "{{ trigger.event.data.text }}"
```

Пример «пропущенный звонок»:

```yaml
alias: Пропущенный GSM-звонок
trigger:
  - platform: state
    entity_id: binary_sensor.athena_gsm_vkhodyashchiy_zvonok
    from: "on"
    to: "off"
action:
  - service: notify.persistent_notification
    data:
      title: "Пропущенный звонок"
      message: "Перезвоните: {{ state_attr('binary_sensor.athena_gsm_vkhodyashchiy_zvonok','caller') }}"
```

Пример «снимок при движении + SMS»:

```yaml
alias: Камера — движение
trigger:
  - platform: state
    entity_id: binary_sensor.athena_gsm_dvizhenie
    to: "on"
action:
  - service: athena_sim7600.take_snapshot
  - delay: "00:00:02"
  - service: athena_sim7600.send_sms
    data:
      number: "+79991234567"
      text: "Движение у двери"
```

## Карточка Lovelace

```yaml
type: picture-entity
entity: camera.athena_gsm_kamera
camera_image: camera.athena_gsm_kamera
show_state: false
```

Живое MJPEG-видео (без HLS):

```yaml
type: iframe
url: http://192.168.1.50:8099/api/camera/stream.mjpg?token=ВАШ_ТОКЕН
aspect_ratio: "16:9"
```

## Диагностика интеграции

«Устройство → Диагностика» отдаёт статус шлюза, последние 30 событий и конфиг
записи (токен вырезан). Полезно при обращении в issues.

## Обновление

Скопируйте новую версию `custom_components/athena_sim7600`, перезапустите HA.
Опции (интервал опроса, номер SMS по умолчанию) сохраняются.

