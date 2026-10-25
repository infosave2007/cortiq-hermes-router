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
| `globalTiers` | Пусто; массивы для `low`, `medium`, `high` |
| `taskRules` | Пусто; метка задачи → модель/массив либо объект с `low`, `medium`, `high`, `any` (`models` — синоним `any`) |
| `defaultModel` | Не задана; последний кандидат и резерв при ошибке классификатора |
| `echoRouting` | `false`; логировать каждое решение (`task=…;score=…;model=…`) |

Приоритет для классифицированного хода: сначала модели правила **для
разрешённого уровня**; на `high` общий список `high` идёт перед `any` задачи
(эскаляция никогда не занижается), на `low`/`medium` `any` задачи идёт первым;
`defaultModel` — последний кандидат. Дубликаты отбрасываются, пустые записи
игнорируются. Тир назначает классификатор — решения берутся как есть.

## Слеш-команда

```text
/cortiq-router status            конфиг, ключи, последнее решение
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
python3 -m unittest discover -s tests -t .     # чистая логика выбора/конфига/классификации
cd standalone && npm run check                 # lint + build + 61 юнит-тест
hermes plugins validate .                      # каталог-гейт: probe + security scan
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