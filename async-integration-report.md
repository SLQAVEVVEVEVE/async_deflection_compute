# Интеграция async‑сервиса (HTTP callback) с текущим Rails сервисом

Цель: подключить внешний асинхронный сервис расчёта (отложенное действие ~5–10 секунд) к текущему Ruby on Rails приложению по HTTP, чтобы результат возвращался обратно в Rails через callback и отображался в UI/API.

---

## 1) Repo overview

### Что это за приложение

- Rails: `8.0.3` (см. `Gemfile`, `Gemfile.lock`)
- Ruby: `3.3.9` (см. `.ruby-version`)
- Назначение: расчёт прогиба балок через Web UI + REST JSON API + Swagger.

### Как запускается локально

**Docker Compose (рекомендуемый для dev в проекте)**

- `docker-compose.yml` поднимает сервисы: `nginx`, `web` (Rails), `frontend` (Vite), `db` (PostgreSQL), `redis`, `minio`.
- Входная точка UI (nginx, TLS): `https://localhost:8080` (self‑signed).
- Rails API (Puma с TLS): `https://localhost:3000`.
- Reverse proxy: `nginx/nginx.conf`
  - `/api/`, `/api-docs/`, `/api-json/`, `/cart` → `https://web:3000`
  - `/` → `https://frontend:5173` (Vite dev server + HMR websocket)
- Puma TLS включается через переменные окружения в `docker-compose.yml`:
  - `RAILS_SSL=true`
  - `SSL_CERT_PATH=/etc/rails/certs/cert.crt`
  - `SSL_KEY_PATH=/etc/rails/certs/cert.key`
  - см. `config/puma.rb`

**Без Docker**

- `README.md` описывает запуск:
  - `bundle install`
  - `bin/rails db:prepare`
  - `bin/rails s`
- В проекте также есть `bin/dev` и `bin/setup` (для dev‑окружения).

### Хранилища и зависимости

- PostgreSQL 15 (основная БД): `docker-compose.yml`
- Redis 7 (JWT blacklist; не блокирует логин при падении Redis): `config/initializers/redis.rb`, `app/services/jwt_blacklist.rb`
- MinIO (S3‑совместимое хранилище для изображений балок): `config/initializers/minio.rb`

---

## 2) Домен: “заявка” / основной объект

В текущем проекте “заявка” соответствует модели `BeamDeflection`.

### Модели

- `app/models/beam_deflection.rb` — заявка/заказ на расчёт.
- `app/models/beam_deflection_beam.rb` — join‑модель (M‑M) между заявкой и балками.
- `app/models/beam.rb` — справочник балок (с параметрами расчёта).
- `app/models/user.rb` — пользователь, роль модератора.

### Таблицы и поля (из `db/schema.rb`)

**`beam_deflections`**

- `status` (string, default `draft`, NOT NULL)
- `creator_id` (bigint, NOT NULL), `moderator_id` (bigint, NULL)
- `formed_at`, `completed_at` (datetime)
- `note` (text, NULL)
- `within_norm` (boolean, NULL)
- `result_deflection_mm` (decimal(18,6), NULL) — агрегированный результат
- `calculated_at` (datetime, NULL)
- Есть уникальный partial index: один `draft` на пользователя (по `creator_id`).
- Есть check constraint на допустимые значения `status`.

**`beam_deflections_beams`** (join‑таблица)

- `beam_deflection_id`, `beam_id` (NOT NULL)
- `quantity` (int, default 1)
- `position` (int, default 1)
- `length_m` (decimal(8,3), NULL) — вход для расчёта
- `udl_kn_m` (decimal(8,3), NULL) — вход для расчёта
- `deflection_mm` (decimal(18,6), NULL) — **per‑item результат** (поле результата в M‑M)

### Статусы / state machine

В коде используется набор строковых статусов:

- `draft`, `formed`, `completed`, `rejected`, `deleted` — `BeamDeflection::STATUSES` в `app/models/beam_deflection.rb`.

Смена меток времени:

- `before_save :set_timestamps` проставляет `formed_at` при `formed` и `completed_at` при `completed`.

### Связи

`BeamDeflection`:

- `belongs_to :creator, class_name: 'User'`
- `belongs_to :moderator, class_name: 'User', optional: true`
- `has_many :beam_deflection_beams`
- `has_many :beams, through: :beam_deflection_beams`

### Валидации / callbacks

`BeamDeflection`:

- валидирует `status` по списку, `creator_id` presence
- `validate_single_draft_per_user` (плюс дублирующее ограничение индексом)
- `before_validation :set_default_status` (ставит `draft` на create)

`BeamDeflectionBeam`:

- валидирует `quantity > 0`, `length_m > 0` (allow_nil), `udl_kn_m >= 0` (allow_nil)
- уникальность `beam_deflection_id` в scope `beam_id`
- “backward compatibility” блок ставит дефолты, если nil

### Где считается результат (сейчас синхронно)

`BeamDeflection#compute_result!` (в `app/models/beam_deflection.rb`):

- по каждой позиции (`beam_deflection_beams`) считает `Calc::Deflection.call(item, item.beam)`
- пишет per‑item `beam_deflections_beams.deflection_mm`
- агрегирует `result_deflection_mm` (с учётом `quantity`)
- считает `within_norm` и пишет `calculated_at`

Формула: `app/services/calc/deflection.rb` (расчёт прогиба при равномерной нагрузке, перевод единиц).

---

## 3) Где происходит “завершение/одобрение/смена статуса”

### API (основной рабочий путь)

Маршруты: `config/routes.rb` → `namespace :api` → `resources :beam_deflections`

Ключевые действия:

- `PUT /api/beam_deflections/:id/form` → `Api::BeamDeflectionsController#form`
  - `draft` → `formed`
  - проверки: владелец, корзина не пустая, заполнены `length_m`/`udl_kn_m` по всем item
- `PUT /api/beam_deflections/:id/complete` → `Api::BeamDeflectionsController#complete`
  - только модератор, только из `formed`
  - сейчас синхронно делает `compute_result!` и ставит `completed`
- `PUT /api/beam_deflections/:id/reject` → `#reject`
  - модератор, `formed` → `rejected`
- `DELETE /api/beam_deflections/:id` → `#destroy`
  - владелец, soft‑delete: `deleted`

### Web UI (дублирующий путь)

- `POST /orders/:id/complete` → `OrdersController#complete`
  - ставит `completed` и вызывает `compute_and_store_result_deflection!` → `compute_result!`
  - в коде есть комментарий, что “в реальном приложении” нужно добавить авторизацию модератора.

### Сброс результата при изменении входных данных

- `PUT /api/beam_deflections/:beam_deflection_id/items/update_item` → `Api::BeamDeflectionItemsController#update_item`
  - при изменении `length_m`/`udl_kn_m` сбрасывает per‑item `deflection_mm` и агрегатные поля заявки (`result_deflection_mm`, `within_norm`, `calculated_at`) в `nil`.

---

## 4) Текущие API endpoints (важно для интеграции)

### Base path и формат

- Base path: `/api`
- Версионирование отсутствует (`/api/v1` нет).
- Swagger: `/api-docs` (UI), `/api-json` (schema).
- Рендер JSON — через `render json:` (без Jbuilder в этих контроллерах).

### Endpoints для “заявок” (`beam_deflections`)

| METHOD | PATH | controller#action | auth | params | response |
|---|---|---|---|---|---|
| GET | `/api/beam_deflections` | `Api::BeamDeflectionsController#index` | Bearer JWT | `status`, `from`, `to`, `page`, `per_page` | `beam_deflections: [...]`, `meta: {...}` |
| GET | `/api/beam_deflections/:id` | `#show` | Bearer JWT (owner/moderator) | `:id` | полная карточка + `items[]` |
| PUT | `/api/beam_deflections/:id` | `#update` | Bearer JWT (owner) | `beam_deflection[note]` | карточка |
| PUT | `/api/beam_deflections/:id/form` | `#form` | Bearer JWT (owner) | — | карточка |
| PUT | `/api/beam_deflections/:id/complete` | `#complete` | Bearer JWT (moderator) | — | карточка |
| PUT | `/api/beam_deflections/:id/reject` | `#reject` | Bearer JWT (moderator) | — | карточка |
| DELETE | `/api/beam_deflections/:id` | `#destroy` | Bearer JWT (owner) | — | `204` |

---

## 5) Авторизация/роли

### Как аутентифицируется пользователь

- API использует JWT Bearer:
  - разбор `Authorization` и установка `Current.user` — `app/controllers/api/base_controller.rb`
  - encode/decode — `app/lib/jwt_token.rb`
- JWT blacklist хранится в Redis:
  - `config/initializers/redis.rb`
  - `app/services/jwt_blacklist.rb`

### Роль модератора

- `users.moderator` (boolean) + `User#moderator?` (`app/models/user.rb`)
- API‑проверка: `require_moderator!` в `Api::BaseController`.

### Псевдо‑авторизация для callback

- Для callback от async‑сервиса лучше отдельный секрет (ENV), а не пользовательский JWT:
  - пример: `X-Async-Token: <ASYNC_CALLBACK_TOKEN>`
- Более строгий вариант (опционально): HMAC‑подпись (`X-Signature`) + timestamp.

---

## 6) Точка интеграции async‑сервиса (с учётом принятых решений)

### Принятые решения (по вашим ответам)

1) Статус заявки ставим **сразу** (не ждём async результата).
2) Async‑результат включает **per‑item** значения (обновляем `beam_deflections_beams.deflection_mm`).
3) Корреляция по существующему `beam_deflection.id` (отдельный `job_id` не обязателен).
4) В async‑сервис передаём **все входные параметры и константы**, необходимые для расчёта (не полагаемся на доступ к БД Rails).
5) Async‑сервис — **вне Docker**, отдельный процесс/репозиторий.
6) Rails остаётся на `https://<host>:3000` (как сейчас).
7) Не усиливаем web‑авторизацию “complete” (UI должен быть виден только модератору).
8) UI будет **поллить** заявку до появления результата.
9) При сбоях/задержке результата — значение просто обновится позже (UI продолжает ожидание/обновление).
10) Безопасность callback пока не финализирована.

### Предложение: trigger request из Rails в async‑сервис

**Когда триггерить**

- API: `Api::BeamDeflectionsController#complete` (`PUT /api/beam_deflections/:id/complete`)
- Web: `OrdersController#complete` (`POST /orders/:id/complete`) — чтобы web‑путь не расходился с API‑логикой.

**Что отправлять**

- Рекомендуемый payload (всё, что нужно для расчёта, включая параметры балок):

```json
{
  "beam_deflection_id": 123,
  "items": [
    {
      "beam_id": 10,
      "quantity": 2,
      "length_m": 6.0,
      "udl_kn_m": 3.5,
      "beam": {
        "elasticity_gpa": 12.0,
        "inertia_cm4": 1500.0,
        "allowed_deflection_ratio": 250
      }
    }
  ]
}
```

- Если важно “заморозить” константы формулы, можно явно зафиксировать версию формулы/алгоритма:
  - `algorithm: "deflection_v1"` или `formula_version: 1`.

### Предложение: callback endpoint в Rails (приём результата)

**METHOD+PATH**

- `POST /api/beam_deflections/:id/async_result`

**Auth**

- `X-Async-Token: <ASYNC_CALLBACK_TOKEN>`

**JSON body (per‑item + агрегат)**

```json
{
  "beam_deflection_id": 123,
  "calculated_at": "2025-12-17T11:30:00Z",
  "within_norm": true,
  "result_deflection_mm": 123.456789,
  "items": [
    { "beam_id": 10, "deflection_mm": 10.123456 }
  ]
}
```

**Ответы**

- `200 OK` — принято/обновлено
- `401 Unauthorized` — неверный `X-Async-Token`
- `404 Not Found` — заявка не найдена/удалена
- `422 Unprocessable Entity` — невалидный payload (нет обязательных полей/неверные типы)

### Поведение API/UI при “complete”

С учётом решения “статус сразу”:

- `PUT .../complete`:
  - ставит `status=completed`, `moderator`, `completed_at`
  - **не** заполняет `result_deflection_mm` и per‑item `deflection_mm` немедленно (они остаются `null`)
  - инициирует HTTP‑запрос в async‑сервис (best effort)
- UI:
  - после `complete` продолжает опрашивать `GET /api/beam_deflections/:id` до появления `result_deflection_mm` (и per‑item результатов).

---

## 7) Что именно должно меняться в данных

**Основное поле (1 поле):** `beam_deflections.result_deflection_mm` (decimal(18,6), nullable)

- заполняется асинхронно callback’ом
- уже отображается/возвращается в `GET /api/beam_deflections` и `GET /api/beam_deflections/:id`

**Дополнительно по решению №2:** обновляются per‑item результаты

- `beam_deflections_beams.deflection_mm` (decimal(18,6), nullable) — по каждой позиции.

---

## 8) Список прочитанных файлов

Версии/запуск:

- `.ruby-version`
- `Gemfile`
- `Gemfile.lock`
- `README.md`
- `docker-compose.yml`
- `dockerfile`
- `config/puma.rb`
- `nginx/nginx.conf`
- `config.ru`
- `bin/rails`, `bin/dev`, `bin/setup`

Домен/БД:

- `db/schema.rb`
- `app/models/beam_deflection.rb`
- `app/models/beam_deflection_beam.rb`
- `app/models/beam.rb`
- `app/models/user.rb`
- `app/services/calc/deflection.rb`

API/контроллеры/авторизация:

- `config/routes.rb`
- `app/controllers/api/base_controller.rb`
- `app/controllers/api/auth_controller.rb`
- `app/controllers/api/beam_deflections_controller.rb`
- `app/controllers/api/beam_deflection_items_controller.rb`
- `app/controllers/orders_controller.rb`
- `app/controllers/carts_controller.rb`
- `app/controllers/application_controller.rb`
- `app/lib/jwt_token.rb`
- `app/services/jwt_blacklist.rb`
- `config/initializers/redis.rb`
- `config/initializers/minio.rb`

Git:

- `git log -1 --oneline`

---

## Open questions (осталось уточнить)

1) Какой точный base URL async‑сервиса (host/port) и протокол (HTTP/HTTPS)?
2) Нужна ли строгая идемпотентность callback: допускаем повторную доставку того же результата, или нужен `request_hash`/`calculated_at` контроль?
3) Что делать, если callback пришёл для заявки в неподходящем состоянии (например, ещё `formed` или уже `deleted`)?
4) Какой формат ошибок/ретраев у async‑сервиса: сколько попыток и интервалы, чтобы UI “дождался” результата?
5) Должен ли callback обновлять также `within_norm` и `calculated_at` (в предложении — да), или эти поля не нужны?
6) Нужна ли защита callback сильнее статического токена (HMAC‑подпись/allowlist IP/timestamp)?
7) Нужно ли логировать и сохранять “сырой ответ” async‑сервиса (для отладки) и где (таблица/лог)?
