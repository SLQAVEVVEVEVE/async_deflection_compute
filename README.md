# Async deflection service (Django) — Lab 8

Сервис выполняет «долгий» расчёт прогиба (задержка 5–10 секунд) и после завершения отправляет результат в основной сервис (Rails) по HTTP callback (без доступа к БД Rails).

## Запуск

Windows (PowerShell):

```powershell
python -m venv env
.\env\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py runserver 0.0.0.0:8001
```

Linux/macOS:

```bash
python -m venv env
. env/bin/activate
pip install -r requirements.txt
python manage.py runserver 0.0.0.0:8001
```

## Конфигурация (env)

- `MAIN_SERVICE_BASE_URL` (default: `https://localhost:3000`)
- `MAIN_SERVICE_ASYNC_RESULT_PATH` (default: `/api/beam_deflections/{beam_deflection_id}/async_result`)
- `CALLBACK_AUTH_TOKEN` (default: `12345678`)
- `CALLBACK_AUTH_HEADER` (default: `X-Async-Token`)
- `CALLBACK_AUTH_SCHEME` (default: пусто; например `Bearer`)
- `MAIN_SERVICE_TIMEOUT_SECONDS` (default: `10`)
- `MAIN_SERVICE_VERIFY_TLS` (default: `true`; для self-signed можно поставить `false`)
- `ASYNC_DELAY_MIN_SECONDS` (default: `5`)
- `ASYNC_DELAY_MAX_SECONDS` (default: `10`)
- `ASYNC_MAX_WORKERS` (default: `5`)

## API

### `POST /api/v1/calculate-deflection/`

Триггер из Rails: запускает асинхронный расчёт и сразу отвечает `202`.

Пример запроса:

```bash
curl -X POST http://localhost:8001/api/v1/calculate-deflection/ \
  -H "Content-Type: application/json" \
  -d "{
    \"beam_deflection_id\": 123,
    \"items\": [
      {
        \"beam_id\": 10,
        \"quantity\": 2,
        \"length_m\": 6.0,
        \"udl_kn_m\": 3.5,
        \"beam\": {
          \"elasticity_gpa\": 12.0,
          \"inertia_cm4\": 1500.0,
          \"allowed_deflection_ratio\": 250
        }
      }
    ]
  }"
```

### `GET /api/health/`

Проверка доступности сервиса.

## Callback в Rails

После задержки сервис отправляет `POST`:

`{MAIN_SERVICE_BASE_URL}{MAIN_SERVICE_ASYNC_RESULT_PATH}`

Тело:

```json
{
  "beam_deflection_id": 123,
  "calculated_at": "2025-12-17T11:30:00Z",
  "within_norm": true,
  "result_deflection_mm": 123.456789,
  "items": [{ "beam_id": 10, "deflection_mm": 10.123456 }]
}
```

Псевдо-авторизация:

- по умолчанию: `X-Async-Token: 12345678`
- на стороне Rails достаточно сравнить токен с ENV/константой и вернуть `401/403`, если не совпало

