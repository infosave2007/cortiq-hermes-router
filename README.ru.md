# Автоматический выбор модели для Hermes

[English](README.md)

[![CI](https://github.com/infosave2007/cortiq-hermes-router/actions/workflows/ci.yml/badge.svg)](https://github.com/infosave2007/cortiq-hermes-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hermes plugin](https://img.shields.io/badge/Hermes-plugin-6f42c1.svg)](https://hermes-agent.nousresearch.com/docs)
[![Node.js](https://img.shields.io/badge/Node.js-%3E%3D22-339933?logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)

**Плагин Hermes, который подбирает нужную модель на каждом ходу.**
Ваши правила отправляют рутину недорогой модели, а сложные задачи — сильной:
[AllaiGate](https://api.allaigate.com/ru/) классифицирует запрос (тип задачи и
сложность 0…1), плагин применяет семейную схему маршрутизации
(`taskRules` → `globalTiers` → `defaultModel`) и **внутри процесса Hermes**
подменяет исходящую модель. Без прокси, без порта, без второго демона.

Пример правил — модели выбираете вы:

| Задача и оценка | Ваш выбор |
| --- | --- |
| Простая задача, низкая сложность | Ваша недорогая модель |
| Сложная задача с кодом | Ваша более сильная модель |

В [живом тесте 25 октября 2026 года](docs/live-verification.md) три реальных
промпта ушли по тирам low → medium → high: `xiaomi/mimo-v2.6-flash`
(score 0.2513) → `anthropic/claude-haiku-5.5` (0.3393) →
`moonshotai/kimi-k3.1-pro` (0.6061); каждое completion подтверждено полем
`model` в SSE-ответе провайдера.

## Как это работает

```text
   вы: "переведи это…"                       вы: "спроектируй exactly-once дедуп…"
         │                                              │
         ▼                                              ▼
┌──────────────────────── ход Hermes ────────────────────────────────┐
│  cortiq-hermes-router · middleware llm_request (in-process)        │
│                                                                    │
│   1. классификация  POST {routerUrl}/v1/route  ← CORTIQ_ROUTER_KEY  │
│        → { task, complexity.score, complexity.tier }                │
│   2. выбор          taskRules → globalTiers → defaultModel          │
│        (complexityBands — опция; по умолчанию tier роутера)         │
│   3. подмена        request.model = выбранная модель                │
│        классификатор недоступен → defaultModel → текущая модель     │
└───────────────┬────────────────────────────────────┬───────────────┘
                ▼                                    ▼
      xiaomi/mimo-v2.6-flash              moonshotai/kimi-k3.1-pro
        (тир low)                              (тир high)
                │                                    │
                └──────────► OpenRouter ◄────────────┘
                          (ваш провайдер, без изменений)
```

- **Классификация ограничена**: на роутер уходит только последнее
  пользовательское сообщение, обрезанное до `maxChars`, с дедлайном
  `timeoutMs`; повторные ходы используют внутренний кеш решений.
- **Провайдер не трогаем**: `model.provider`, ключи и `base_url` остаются как
  настроены — меняется только идентификатор `model` внутри запроса и только
  когда классификатор ответил.
- **Fail-open**: таймаут/HTTP-ошибка/некорректный ответ классификатора →
  ваша `defaultModel`; без `defaultModel` → текущая модель; битый конфиг →
  одно понятное предупреждение и пауза маршрутизации. Ход никогда не падает
  из-за роутинга.
- **С машины уходит только текст классификации** (не длиннее `maxChars`) в
  указанный вами роутер.

## Установка

Нужны работающий [Hermes Agent](https://hermes-agent.nousresearch.com/docs)
и API-ключ AllaiGate.

### Одна команда (рекомендуется)

```sh
hermes plugins install infosave2007/cortiq-hermes-router
hermes plugins enable cortiq-hermes-router
```

`install` принимает Git-URL или сокращение `owner/repo`, запускает security
scan и проверку capabilities, показывает экран согласия на объявленный
capability `llm.model_override` («выбор модели для LLM-вызовов хоста»).
Закрепите неизменяемый коммит флагом `--ref <SHA 40 символов>`; в
неинтерактивной установке capabilities остаются не выданными, пока вы не
запустите `hermes plugins capabilities cortiq-hermes-router`.

### Вручную (Git-чекаут)

```sh
git clone https://github.com/infosave2007/cortiq-hermes-router.git \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/cortiq-hermes-router"
hermes plugins enable cortiq-hermes-router
```

### В официальный каталог

Курируемый каталог лежит в
[`NousResearch/hermes-agent` `plugin-catalog/`](https://github.com/NousResearch/hermes-agent).
Запись `plugin-catalog/cortiq-hermes-router.yaml` (поля: `name`, `repo`, `sha`
— пин 40-символьного коммита — `description`, `maintainer`, `tier`,
`category`, `docs_url`, `version`, `capabilities`) подана и ждёт ревью в
[PR #144419](https://github.com/NousResearch/hermes-agent/pull/144419).
После мержа установка по имени: `hermes plugins install cortiq-hermes-router`.
До этого оба способа установки выше работают уже сегодня.

Если gateway уже запущен, перезапустите его **из внешнего шелла**:
`hermes gateway restart`. Команды жизненного цикла gateway заблокированы
внутри чата — не запускайте их из чата.

## Настройка

Бесплатный API-ключ AllaiGate получается на
[api.allaigate.com](https://api.allaigate.com/ru/) — это ключ классификатора
роутера, отдельный от ключей модельного провайдера. Положите его в окружение,
из которого стартует Hermes (никогда в репозиторий):

```sh
echo 'CORTIQ_ROUTER_KEY=ваш-ключ-allaigate' >> ~/.hermes/.env
```

Настройки маршрутизации — в `~/.config/cortiq-hermes-router/config.json`
(другой файл выбирается через `CORTIQ_ROUTER_CONFIG`). За отправную точку
берите [cortiq-router.example.json](cortiq-router.example.json):

```json
{
  "globalTiers": {
    "low": ["xiaomi/mimo-v2.6-flash"],
    "medium": ["anthropic/claude-haiku-5.5"],
    "high": ["moonshotai/kimi-k3.1-pro"]
  },
  "taskRules": {},
  "defaultModel": "xiaomi/mimo-v2.6-flash"
}
```

Идентификаторы моделей должны подходить **вашему провайдеру** (для OpenRouter —
`provider/model`, например `anthropic/claude-haiku-5.5`).

| Настройка | Значение по умолчанию / смысл |
| --- | --- |
| `enabled` | `true`; явный `false` отключает дальнейшую маршрутизацию |
| `apiKeyEnv` | `CORTIQ_ROUTER_KEY` |
| `routerUrl` | `https://router.allaigate.com`; добавляется `/v1/route` |
| `taxonomyId` | `data-assistant` |
| `timeoutMs` | `15000`; целое 1–120000 мс |
| `maxChars` | `12000`; целое 1–1000000 кодовых единиц UTF-16 |
| `routerProfile` | `balanced`, `cost-saver` или `quality-first` |
| `complexityBands` | Опущено (по умолчанию): используется tier роутера. Опционально `{ "low": 0.35, "medium": 0.65 }` задаёт свои границы баллов |
| `globalTiers` | Пусто; массивы для `low`, `medium`, `high` из строк-моделей или объектов endpoint'ов (см. [Провайдеры](#провайдеры-и-локальные-модели)) |
| `taskRules` | Пусто; метка задачи → модель/массив либо объект с `low`, `medium`, `high`, `any` (`models` — синоним `any`) |
| `defaultModel` | Не задана; последний кандидат и резерв при ошибке классификатора |
| `echoRouting` | `false`; логировать каждое решение (`task=…;score=…;model=…`) |

Приоритет для классифицированного хода: сначала модели правила **для
разрешённого уровня**; на `high` общий список `high` идёт перед `any` задачи
(эскаляция никогда не занижается), на `low`/`medium` `any` задачи идёт первым;
`defaultModel` — последний кандидат. Дубликаты отбрасываются, пустые записи
игнорируются. Тир назначает классификатор — решения берутся как есть.

## Провайдеры и локальные модели

Запись тира — это либо **строка** (id модели OpenRouter, идёт через собственный
клиент Hermes, как и в 0.2.x), либо **объект**, который задаёт свой endpoint:

```json
{
  "globalTiers": {
    "low": [
      { "provider": "ollama", "model": "qwen3:8b" },
      "xiaomi/mimo-v2.6-flash"
    ],
    "medium": [
      { "provider": "lmstudio", "model": "qwen2.5-coder-14b-instruct" },
      "anthropic/claude-haiku-5.5"
    ],
    "high": [
      { "provider": "cortiq", "model": "qwen38-27b-q4tp", "baseUrl": "http://127.0.0.1:8080/v1" },
      { "provider": "openai", "model": "gpt-4.1", "apiKeyEnv": "OPENAI_API_KEY" }
    ]
  },
  "taskRules": {
    "coding": {
      "low": [{ "provider": "vllm", "model": "Qwen/Qwen2.5-Coder-7B", "baseUrl": "http://127.0.0.1:9000/v1" }],
      "any": ["anthropic/claude-haiku-5.5"]
    }
  },
  "defaultModel": "xiaomi/mimo-v2.6-flash"
}
```

Для `cortiq` используйте id, который сервер возвращает в `GET /v1/models`.
Пресет `cortiq` — `http://127.0.0.1:8080/v1` (порт по умолчанию у `cortiq serve`).

| Поле | Значение |
| --- | --- |
| `provider` | обязательно; имя пресета или любая метка для явного `baseUrl` |
| `model` | обязательно; id, который ждёт endpoint (префикс `provider/` для локальных серверов не нужен) |
| `baseUrl` | `http(s)://…/v1`; берётся из пресета; **обязателен** для `vllm`, `llamacpp` и неизвестных провайдеров |
| `apiKeyEnv` | **имя** переменной окружения с ключом — не сам ключ |
| `apiMode` | `chat_completions` (по умолчанию) или `anthropic_messages` |
| `timeoutMs` | по умолчанию `60000`; максимальная пауза между байтами, 1–120000 |
| `extraBody` | объект, добавляемый в тело запроса; не может задавать `model`, `messages`, `stream` |

Пресеты (`provider` → `baseUrl`, переменная ключа):

| Провайдер | Base URL | Переменная ключа |
| --- | --- | --- |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `http://127.0.0.1:11434/v1` | — |
| `lmstudio` | `http://127.0.0.1:1234/v1` | — |
| `cortiq` | `http://127.0.0.1:8080/v1` | — |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `together` | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` |
| `anthropic` | `https://api.anthropic.com` (задать `apiMode: anthropic_messages`) | `ANTHROPIC_API_KEY` |
| `vllm`, `llamacpp` | нет — укажите `baseUrl` | необязательно |

**Валидация** строгая, как и остальной конфиг: неизвестные поля, `baseUrl` без
`http(s)://`, учётные данные в URL, `model` с пробелами, а также открытый
`http` с `apiKeyEnv` на не-loopback хосте — ошибки конфигурации (маршрутизация
ставится на паузу, модель сессии сохраняется). Отсутствующая переменная ключа —
**предупреждение**, и на этапе вызова выполняется откат. Дубликаты записей
выводятся как предупреждения. Ключи не попадают в конфиг, логи, статистику и
вывод команд.

**Как выполняются прямые вызовы (`llm_execution`).** Объектные записи выполняет
сам плагин на своём endpoint; инструменты поддерживаются в обоих API-режимах.
Откат (fail-open) безусловный: при любой ошибке запрос повторяется через модель,
которую Hermes собирался использовать, а причина учитывается в статистике:
`timeout`, `connection`, `http_<статус>`, `invalid_json`, `invalid_stream`,
`stream_error`, `invalid_response`, `empty_response`, `missing_key`,
`unsupported_request`, `provider_override_not_granted`, `internal_error`.

- Hermes по умолчанию запрашивает стрим. Прямые вызовы тоже идут стримом к
  endpoint, но ответ собирается целиком до передачи Hermes, поэтому для
  маршрутизируемых прямых вызовов не показывается потокового вывода по токенам.
- Вызовы `anthropic_messages` к endpoint — без стрима; изображения не конвертируются.
- Опции запроса, специфичные для OpenRouter (`extra_body`, предпочтения провайдера),
  на прямые endpoint'ы не передаются; нужное endpoint'у задавайте в `extraBody`.
- Прямым endpoint'ам нужна capability **`llm.provider_override`**. Она объявлена
  при установке (экран согласия). Установка, обновлённая с 0.2.x, должна получить
  её явно: `hermes plugins capabilities cortiq-hermes-router`. До этого прямые
  записи пропускаются с однократным предупреждением, а модель **не** подменяется
  локальным id.
- Автономный прокси из `standalone/` принимает только строковые записи.

**Проверка endpoint'ов без расхода токенов**

```text
/cortiq-router providers              GET {baseUrl}/models, таймаут 3 с, без completion;
                                      ключ показывается только как «set» или «MISSING»
/cortiq-router test <тир|провайдер>   один промпт, max_tokens=8; только по вашей команде
```

## Контроль стоимости

Один ход пользователя может сделать десятки вызовов провайдера (цикл
инструментов). Роутер классифицирует ход один раз и не переклассифицирует
каждый шаг инструмента:

| Вызов внутри хода (`api_call_count`) | Выбор модели |
| --- | --- |
| `1` (первый вызов) | классификация, затем тир и `taskRules` как обычно |
| `2` и далее (продолжение цикла инструментов) | **без вызова классификатора**; первая запись `globalTiers.low`, а если она пуста — `defaultModel`; `taskRules` не учитываются |
| новый ход (новый `turn_id`) | классификация заново |

Повторы внутри одного хода используют решение этого хода из кэша. Если
`api_call_count` не передан, вызов считается первым.

Выбирайте запись `low` осознанно: она обслуживает все шаги инструментов длинных
ходов. Берите самую дешёвую модель, которая надёжно формирует вызовы инструментов.
В статистике шаги инструментов видны как `step_continuation_count` (итоги и по
дням), колонка `Tool step` в `stats recent` и статус `step_continuation`.

## Слеш-команда

```text
/cortiq-router status            конфиг, ключи, последнее решение
/cortiq-router stats             today/week/all + разбивки за всё время
/cortiq-router stats today|week|all
/cortiq-router stats recent [N]  последние N решений (10 по умолчанию, макс. 200)
/cortiq-router stats --json      полный машиночитаемый агрегат
/cortiq-router stats reset       безопасный запрос подтверждения
/cortiq-router stats reset yes   очистить статистику, сохранить last_decision
/cortiq-router providers              configured endpoints: GET {baseUrl}/models, без completion, ключи скрыты
/cortiq-router test <тир|провайдер>   один дешёвый промпт через этот endpoint (только по команде)
/cortiq-router decide <промпт>   классификация без маршрутизации (JSON)
/cortiq-router on | off          вкл/выкл маршрутизацию профиля
/cortiq-router help              справка
```

Формат вывода `decide`:

```json
{
  "task": "qa", "score": 0.3393, "tier": "medium",
  "model": "anthropic/claude-haiku-5.5",
  "candidates": ["anthropic/claude-haiku-5.5"],
  "fallback": false, "classifier": "ok",
  "source": "router", "requestId": "req_…", "profile": "balanced"
}
```

## Статистика

Каждый вызов middleware `llm_request` добавляет одну запись решения **без
текста запроса**: UTC timestamp, task, score, tier, выбранную и исходную модели,
fallback, status и classifier latency, если классификатор реально вызывался.
Попадания в кеш тоже учитываются; их latency для этого вызова равна `null`.
Тексты запросов и ответов не сохраняются. Запись идёт через атомарный
plugin-state bridge Hermes; ошибка state не ломает маршрутизацию, а решение
остаётся в агрегате в памяти.

Агрегат содержит totals за всё время, такие же totals для каждого дня UTC и
ring buffer recent на 200 решений. `week` — текущий день UTC плюс предыдущие
шесть дней. Старый `last_decision` из v0.1 мигрирует один раз и не удаляется;
`stats reset yes` очищает только статистику и тоже сохраняет `last_decision`.

Статусы: `ok`, `classifier_error` (HTTP, нет ключа или невалидный ответ),
`classifier_timeout`, `invalid_config` и `disabled` (маршрутизация выключена,
capability не выдана или нет классифицируемого запроса). Это решения плагина,
а не подтверждения ответа upstream: в собственном логе Hermes может остаться
исходная модель.

```text
/cortiq-router stats

cortiq-hermes-router statistics (UTC; week = today + previous 6 days)

Period  Calls  Fallback  Avg score  Avg cls ms  Max cls ms
------  -----  --------  ---------  ----------  ----------
today   685    0         0.4176     1731.1      9840.2
week    1485   0         0.4184     1558.5      9840.2
all     1485   0         0.4184     1558.5      9840.2

All time · By tier
Name    Count
------  -----
low     1445
medium  29
high    11

All time · By task
Name      Count
--------  -----
(none)    1354
chitchat  102
code      22
qa        7

All time · By model
Name                        Count
--------------------------  -----
xiaomi/mimo-v2.6-flash      1445
anthropic/claude-haiku-5.5  29
moonshotai/kimi-k3.1-pro    11

All time · By status
Name               Count
-----------------  -----
step_continuation  1354
ok                 131
```

`stats today`, `stats week` или `stats all` выбирает период и для сводки, и для
четырёх разбивок. `recent` показывает сначала новые решения:

```text
/cortiq-router stats recent 2

cortiq-hermes-router recent decisions (newest first, UTC)
Timestamp                 Status  Task      Score   Tier    Model                       Fallback  Cls ms  Original model          Provider  Tool step
------------------------  ------  --------  ------  ------  --------------------------  --------  ------  ----------------------  --------  ---------
2026-10-26T07:09:18.742Z  ok      code      0.4796  medium  anthropic/claude-haiku-5.5  no        2855.1  xiaomi/mimo-v2.6-flash  -         no
2026-10-26T07:04:35.657Z  ok      chitchat  0.4522  medium  anthropic/claude-haiku-5.5  no        9840.2  xiaomi/mimo-v2.6-flash  -         no
```

`stats --json` выводит полный объект с ключами `version`, `timezone`, `totals`,
`daily` и `recent`. Сокращённый пример `totals` (дневные bucket-ы имеют ту же
форму агрегата):

```json
{
  "calls": 1485,
  "by_tier": {"low": 1445, "medium": 29, "high": 11},
  "by_task": {"(none)": 1354, "chitchat": 102, "code": 22, "qa": 7},
  "by_model": {"xiaomi/mimo-v2.6-flash": 1445, "anthropic/claude-haiku-5.5": 29, "moonshotai/kimi-k3.1-pro": 11},
  "by_status": {"step_continuation": 1354, "ok": 131},
  "fallback_count": 0,
  "avg_score": 0.418444,
  "score_count": 131,
  "avg_classifier_latency_ms": 1558.541,
  "classifier_latency_count": 131,
  "max_classifier_latency_ms": 9840.241
}
```

Полный пример полей одной записи `recent`:

```json
{
  "timestamp": "2026-10-26T07:09:18.742Z",
  "task": "code",
  "score": 0.47955617,
  "tier": "medium",
  "model": "anthropic/claude-haiku-5.5",
  "originalModel": "xiaomi/mimo-v2.6-flash",
  "fallback": false,
  "status": "ok",
  "classifier_latency_ms": 2855.147,
  "provider": null,
  "step_continuation": false
}
```

Шаги инструментов и прямые endpoint'ы видны в агрегате: `by_provider` считает
маршрутизированные решения по endpoint'ам (`openrouter` для строковых записей),
а `execution_fallback_count`, `fallback_reasons` и `fallback_by_provider`
считают прямые вызовы, по которым сработал откат. `step_continuation_count`
считает шаги инструментов без классификации. Статистика, записанная в 0.2.x,
обновляется на месте.

## Автономный OpenAI-compatible прокси (опционально)

Для харнесов вне Hermes — Codex, Cline, OpenCode, скриптов — те же механики
поставляются как Node-прокси без зависимостей в [standalone/](standalone/):

```sh
cd standalone && npm ci
node dist/server.js serve            # http://127.0.0.1:8791
```

- `POST /v1/chat/completions` классифицируется и маршрутизируется; любой
  другой путь `/v1/*` проходит без изменений (`GET /v1/models` работает).
- Ответы стримятся **байт-в-байт** (`text/event-stream`), добавляется только
  заголовок `X-Cortiq-Router: task=…;score=…;model=…`.
- Соединение с upstream отклонено/таймаут → `502` с
  `{"error":{…,"code":"upstream_unavailable"}}`; процесс остаётся жив.
- Подкоманды: `setup` (печатает точные команды `hermes config set` для
  подключения Hermes к прокси — с показом текущих значений, маскировкой
  ключей и бэкапом в `~/.config/cortiq-hermes-router/hermes-backup.json`;
  `--apply` выполняет их), `decide "<промпт>"` (dry-run JSON), `status`
  (проверка конфига/env/upstream/классификатора). Флаги: `--port`, `--host`,
  `--config`; env конфига: `CORTIQ_ROUTER_CONFIG`.
- Дополнительные ключи конфига **только для прокси**: `host` (по умолчанию
  `127.0.0.1`), `port` (по умолчанию `8791`), `upstream` `{ "url":
  "https://openrouter.ai/api/v1", "apiKeyEnv": "OPENROUTER_API_KEY",
  "timeoutMs": 0 }` (0 — без общего дедлайна). Ключ upstream читается из
  окружения — никогда из файлов репозитория.

Для пользователей Hermes рекомендуется плагин: без порта, без второго
процесса, без переписывания `base_url`.

## Тесты и проверки

```sh
python3 -m unittest discover -s tests -t .     # 136 тестов: выбор, конфиг, провайдеры, HTTP-исполнение, контроль стоимости, статистика, слеш
cd standalone && npm run check                 # lint + build + 61 юнит-тест
hermes plugins validate .                      # каталог-гейт: probe + security scan
hermes plugins doctor .                        # реальные контракты рантайма (импорт + регистрация)
```

Полные реальные доказательства — три тира, параллельные стримы, пустые/
огромные/без `messages` запросы, мёртвый upstream, мёртвый классификатор,
битый конфиг, graceful shutdown — в
[docs/live-verification.md](docs/live-verification.md) (кратко
по-русски — раздел «Кратко по-русски» в конце файла).

## Участие

Issues и PR приветствуются. Держите обе тестовые suites зелёными, запускайте
`hermes plugins validate .` перед PR, никогда не коммитьте секреты (только
имена переменных окружения) и синхронизируйте пару README: английский здесь,
русский — в [README.ru.md](README.ru.md).

## Лицензия

[MIT](LICENSE) © 2026 Cortiq Team.