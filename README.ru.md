# Torrent Name Normalizer

Безопасный индексатор-независимый sidecar, который нормализует **только отображаемое имя** вручную добавленных задач qBittorrent с категорией `tv-sonarr`.

[Полная английская версия / Full English README](README.md)

## Установка

Образ контейнера: `ghcr.io/vladido/torrent-name-normalizer:latest` ([GHCR](https://github.com/vladido/torrent-name-normalizer/pkgs/container/torrent-name-normalizer)).

Для фиксированной версии закрепите доступный тег `sha-<commit>` или digest; перед обновлением сохраните предыдущий digest.

Создайте отдельный каталог установки и сохраните следующий YAML как `compose.yaml` (полный пример из [compose.yaml](compose.yaml)):

```yaml
services:
  torrent-name-normalizer:
    image: ghcr.io/vladido/torrent-name-normalizer:latest
    container_name: torrent-name-normalizer
    restart: unless-stopped
    env_file:
      - ./secrets/normalizer.env
    environment:
      - NORMALIZER_AUDIT_PATH=/audit/normalizer.jsonl
      - NORMALIZER_API_PORT=8077
    expose:
      - "8077"
    volumes:
      - ./config/config.toml:/app/config/config.toml:ro
      - ./audit:/audit
    read_only: true
    tmpfs:
      - /tmp:size=16m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    healthcheck:
      test: ["CMD", "python", "-m", "torrent_normalizer.main", "--healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - servarr

  sonarr-series-auto-add:
    image: ghcr.io/vladido/torrent-name-normalizer:latest
    container_name: sonarr-series-auto-add
    restart: unless-stopped
    entrypoint: ["python", "-m", "torrent_normalizer.auto_add_main"]
    env_file:
      - ./secrets/normalizer.env
    environment:
      - AUTO_ADD_CONFIG=/app/config/auto_add.toml
      - AUTO_ADD_HEALTH_PATH=/tmp/auto-add-health.json
      - AUTO_ADD_AUDIT_PATH=/audit/auto-add.jsonl
      - AUTO_ADD_HEALTH_MAX_AGE_SECONDS=180
      - AUTO_ADD_API_PORT=8078
    expose:
      - "8078"
    volumes:
      - ./config/auto_add.toml:/app/config/auto_add.toml:ro
      - ./audit:/audit
    read_only: true
    tmpfs:
      - /tmp:size=16m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    healthcheck:
      test: ["CMD", "python", "-m", "torrent_normalizer.auto_add_main", "--healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - servarr

networks:
  servarr:
    external: true
    name: ${SERVARR_NETWORK:-servarr}
```

Нужны Docker с Compose и работающие Sonarr/qBittorrent на общей внешней сети `servarr`. При необходимости создайте сеть `docker network create servarr` и подключите к ней зависимости. Другое имя задайте через `SERVARR_NETWORK` в окружении оболочки или `.env` Compose. Оба сервиса запускаются сразу, но остаются в dry-run. Порты `8077` и `8078` доступны только в сети Docker, не на хосте.

```sh
mkdir -p config secrets
umask 077
touch secrets/normalizer.env
chmod 600 secrets/normalizer.env
```

Создайте `config/config.toml`:

```toml
poll_interval_seconds = 60
dry_run = true
max_renames_per_run = 5

[categories]
tv-sonarr = "sonarr"
```

Создайте `config/auto_add.toml`:

```toml
poll_interval_seconds = 60

[auto_add]
dry_run = true
max_adds_per_run = 1
root_folder_path = "/tv"
quality_profile_id = 1
```

Создайте `secrets/normalizer.env`:

```dotenv
QBIT_URL=http://qbittorrent:8080
QBIT_USERNAME=CHANGE_ME
QBIT_PASSWORD=CHANGE_ME
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=CHANGE_ME
PROWLARR_URL=http://prowlarr:9696
PROWLARR_API_KEY=CHANGE_ME
```

Замените `CHANGE_ME` своими данными; `root_folder_path` и `quality_profile_id` должны совпадать с Sonarr. TOML-файлы должны быть доступны контейнеру для чтения (например, `0644`); secrets остаются `0600`. Для ручного запуска добавьте в env `SERVARR_TOOLS_TOKEN` с тем же непустым ASCII-токеном, что у панели. Без него ручной запуск отключён.

Загрузите образ, определите его UID/GID и создайте закрытый каталог аудита с этим владельцем до запуска; для смены владельца нужны права администратора. Эти команды запускают только `id` в одноразовом контейнере и не выполняют цикл обработки:

```sh
chmod 644 config/config.toml config/auto_add.toml
docker compose config --quiet
docker compose pull
runtime_uid=$(docker run --rm --entrypoint id ghcr.io/vladido/torrent-name-normalizer:latest -u)
runtime_gid=$(docker run --rm --entrypoint id ghcr.io/vladido/torrent-name-normalizer:latest -g)
sudo install -d -m 0700 -o "$runtime_uid" -g "$runtime_gid" audit
docker compose up -d
docker compose ps
```

## Выбор сервиса и обычная работа

| Сервис | Назначение | Разрешённые изменения |
|---|---|---|
| `torrent-name-normalizer` | Сериал уже есть в Sonarr, имя задачи не распознаётся | Только отображаемое имя qBittorrent |
| `sonarr-series-auto-add` | Вручную добавленная раздача отсутствующего сериала | Добавление сериала; отдельно — подтверждённое исправление имён папки/файлов |

Добавьте раздачу в qBittorrent с категорией `tv-sonarr`. Для normalizer достаточно однозначного названия и одного сезона; auto-add дополнительно требует явный год и точные эпизоды. Оба процесса по умолчанию работают в dry-run и имеют независимые конфигурации. Просмотрите аудит, затем включайте live только нужного сервиса. Импорт выполняет Sonarr; категории и теги не заменяют доказательство идентичности. Собственного GUI нет; Servarr Tools UI показывает состояние и позволяет поставить цикл в очередь.

## Как работает универсальный resolver

1. qBittorrent предоставляет задачи из category allowlist.
2. Уже распознаваемые Sonarr имена пропускаются.
3. Нормализатор получает текущий каталог через Sonarr `/api/v3/series`, включая `alternateTitles`.
4. Из исходного имени извлекаются варианты названия, явный title-year (если он есть), один сезон и эпизоды.
5. Варианты проверяются через Sonarr `/api/v3/series/lookup` и локальный каталог; явный title-year обязан совпасть с годом локального сериала. Позиция результата в lookup сама по себе не считается доказательством: term должен совпасть с title/alternateTitle либо пройти отдельный уникальный high-confidence transliteration fallback.
6. Разрешены только однозначные соответствия сериалу, уже добавленному в Sonarr.
7. Строится канонический кандидат вида `Title.2027.S02E03-E05`.
8. Sonarr `/api/v3/parse` обязан вернуть тот же series ID, сезон и эпизоды.
9. Только после этих проверок вызывается qBittorrent `/api/v2/torrents/rename`.

В конфигурации и коде нет таблицы названий или regex-замен для отдельных сериалов. Legacy-секция `[[rules]]` намеренно запрещена загрузчиком конфигурации.

## Поддерживаемые структуры

- scene: `Any.Show.S02`, `S02E03`, `S02E03-E05`, `S01E01E03E05`;
- русские маркеры: `Сезон: 2`, `Серия: 5`, `Серии: 1-8`, `Серии: 1, 3, 5`;
- несколько названий через `/`;
- альтернативное название в скобках;
- уникальное высокоточное сопоставление русской транслитерации как последний fallback.

Fuzzy fallback ограничен порогом `0.90` и минимальным отрывом `0.08` от второго результата. Если Sonarr lookup вернул непустой, но нерелевантный набор, разрешён только cross-script Cyrillic↔Latin transliteration fallback; same-script fuzzy запрещён. Точный неустановленный lookup-result блокирует fallback. Любая неоднозначность приводит к `skipped`, а не к угадыванию.

## Границы универсальности

Практическая универсальность означает отсутствие сериал-специфичных правил и автоматическую работу для любого сериала, который можно однозначно сопоставить с текущей библиотекой/алиасами Sonarr.

Автоматическое переименование невозможно и намеренно не выполняется, если:

- сериал отсутствует в Sonarr;
- исходное имя не содержит достаточного названия;
- сезон отсутствует либо задан диапазоном вроде `Сезон: 2-3` или `S01-S02`;
- явный год title не совпадает с годом установленного сериала;
- episode marker присутствует, но диапазон/список неполон, слишком велик, использует неподдерживаемый разделитель или неоднозначен;
- разные варианты названия указывают на разные series ID;
- кандидат не подтверждён Sonarr с тем же сезоном и эпизодами.

## Гарантии

- Источник и трекер не используются как критерий.
- Обрабатываются только категории из `[categories]`.
- Поле qBittorrent `Tags` не используется.
- Уже распознанные Sonarr имена не меняются.
- Меняется только display name через `/api/v2/torrents/rename`.
- Не меняются infohash, файлы, save/content path, category, tags и состояние задачи.
- Не выполняются удаление, перемещение, повторная проверка или управление очередью Sonarr.
- Ошибка одной задачи не останавливает цикл.
- Есть dry-run, лимит live-операций, JSON audit-log и healthcheck.
- Каталог и lookup-term кэшируются только внутри одного poll; следующий poll получает свежие данные.

## Отдельный Sonarr auto-add

Compose также содержит изолированный сервис `sonarr-series-auto-add`. Он не является частью display-name rename-loop. Его единственная qBittorrent mutation — fail-closed canonicalization root/file names для штатного Sonarr import после точного parser proof.

Auto-add рассматривает только задачи с category `tv-sonarr` и работает fail-closed:

1. Пропускает загрузку, если её infohash уже присутствует в Sonarr history или в queue с подтверждённым `seriesId`. Неассоциированная `importBlocked` queue row сама по себе не блокирует canonical repair.
2. Требует один явный год, один сезон и однозначный набор эпизодов.
3. Принимает только единственный exact normalized title/alternate-title результат `/api/v3/series/lookup`; fuzzy matching запрещён.
4. Требует integer TVDB ID, точное совпадение года, наличие сезона в lookup resource и каждого эпизода в SkyHook inventory.
5. Пропускает уже установленный TVDB ID.
6. В live-режиме сначала синхронно пишет owner-only JSONL audit с `fsync`, затем выполняет ровно один `POST /api/v3/series` за цикл.
7. Add payload всегда содержит `searchForMissingEpisodes=false`; release search не запускается. После успешного add вызывается только `RefreshMonitoredDownloads`, чтобы Sonarr повторно рассмотрел уже существующую загрузку.
8. Если exact-title lookup возвращает несколько ремейков разных лет, единственный кандидат с годом, точно совпадающим с единственным явным source year, может снять неоднозначность. Два кандидата того же года остаются ambiguous. Канонический Sonarr title вида `Title (YYYY)` разрешён только при том же подтверждённом годе.

После series add следующий poll может канонизировать root folder и имена каждого стандартного single-episode video через qBittorrent `renameFolder`/`renameFile`, только если display name уже распознаётся в единственный установленный series ID, source year совпадает, все заявленные эпизоды существуют в Sonarr и имеют `hasFile=false`, video inventory точно покрывает заявленные эпизоды, а каждый source и target filename настоящий Sonarr `/api/v3/parse` связывает с тем же series/season/episode. Перед mutation выполняется fresh-state reread, после — exact verification. За цикл выполняется максимум одна series-add или canonical-repair mutation.

При canonical repair не меняются infohash, байты данных, selected files, save path, category, tags, priority, progress или состояние раздачи; seeding сохраняется. Persistent audits находятся в owner-only `audit/normalizer.jsonl` и `audit/auto-add.jsonl`; каталог и файлы исключены из Git. Каждый файл ограничен 8 MiB: после достижения лимита он атомарно сокращается до последних полных 4 MiB после `fsync`, поэтому параллельное чтение API видит либо старый, либо новый целый файл. API читает не более последнего 1 MiB и 2000 строк.

После canonical repair вызывается только `RefreshMonitoredDownloads`; отдельный custom importer не используется, дальнейшее queue handling и импорт выполняет штатный Sonarr.

Safe production-default `config/auto_add.toml`:

```toml
poll_interval_seconds = 60

[auto_add]
dry_run = true
max_adds_per_run = 1
root_folder_path = "/media/series" # пример: замените путём из Sonarr
quality_profile_id = 1
```

`max_adds_per_run` должен быть равен `1`. Перед live необходимо вручную проверить dry-run audit и ещё раз сверить root folder и quality profile с текущим Sonarr.

## Конфигурация normalizer

```toml
poll_interval_seconds = 60
dry_run = true
max_renames_per_run = 5

[categories]
tv-sonarr = "sonarr"
```

Первое развёртывание новой версии всегда выполняйте с `dry_run = true`. После проверки всех `status=dry_run` и отсутствия ложных совпадений можно установить `dry_run = false`.

## Внутренний API v1

Оба процесса поднимают HTTP API только во внутренней сети Docker: normalizer на `8077`, auto-add на `8078`. Прилагаемый Compose явно задаёт эти значения и использует только `expose`, без публикации портов на хосте. Для других портов измените соответствующие `environment` и `expose` вместе; переменная в `.env` сама по себе не меняет этот Compose.

- `GET /v1/health` и `GET /v1/status` — имя сервиса, версия, `dry_run`, флаги `running`/`queued`, последняя метаинформация и сводка запуска; ошибки представлены только именем класса. Поле `/v1/status.last_success_at` равно `null` до первого успешного запуска, затем содержит время его завершения в UTC RFC3339 (`Z`); последующий неуспешный запуск не изменяет это поле.
- `GET /v1/audit?limit=N` — новые сначала, `N` от 1 до 200; выдаются только разрешённые поля, незавершённая последняя JSONL-запись игнорируется. Для безопасно подтверждённых результатов normalizer `renamed`/`dry_run` запись содержит точный положительный `series_id`; пропуски, ошибки и неоднозначные результаты его не получают.
- `POST /v1/runs` — только ставит запуск в единственную worker-loop очередь и возвращает `202`; при занятом worker/очереди или повторном `Idempotency-Key` возвращает `409`.

Для `POST` требуется заголовок `Authorization` со схемой Bearer и значением `SERVARR_TOOLS_TOKEN`. При отсутствующей переменной `run_now=false`, а endpoint возвращает `404`. Заданное значение должно быть непустым printable ASCII без управляющих символов; иначе процесс завершается до загрузки конфигурации. GET не требует токена. Храните токен только в закрытом `secrets/normalizer.env`, вне Git.

Пример из контейнера Servarr Tools в той же сети:

```sh
curl http://torrent-name-normalizer:8077/v1/status
curl 'http://sonarr-series-auto-add:8078/v1/audit?limit=50'
curl -X POST -H "Authorization: $(printf '%s %s' Bearer "$SERVARR_TOOLS_TOKEN")" \
  -H 'Idempotency-Key: ui-request-123' \
  http://torrent-name-normalizer:8077/v1/runs
```

## Секреты

Рабочий файл не отслеживается Git и должен иметь режим `0600`. Несекретный шаблон находится в `secrets/normalizer.env.example`:

```dotenv
QBIT_URL=http://qbittorrent:8080
QBIT_USERNAME=...
QBIT_PASSWORD=...
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=...
PROWLARR_URL=http://prowlarr:9696
PROWLARR_API_KEY=...
```

## Безопасное обновление

1. Установить `dry_run = true` в `config/config.toml` и `config/auto_add.toml`.
2. Выполнить `docker compose pull`, затем `docker compose up -d`.
3. Проверить JSON-события в `docker logs torrent-name-normalizer`, `docker logs sonarr-series-auto-add`, `audit/normalizer.jsonl` и `audit/auto-add.jsonl`.
4. Убедиться, что нет `error`, `resolved_series_mismatch` и неожиданных auto-add candidates.
5. Включать live для каждого сервиса отдельно и только после ручного аудита соответствующего dry-run.
6. Пересоздать нужный контейнер и проверить health/logs повторно.

## Проверка

```sh
docker compose ps
docker logs --tail 100 torrent-name-normalizer
docker logs --tail 100 sonarr-series-auto-add
tail -n 100 audit/normalizer.jsonl
tail -n 100 audit/auto-add.jsonl
docker inspect --format '{{.State.Health.Status}}' torrent-name-normalizer sonarr-series-auto-add
```

## Откат

```sh
docker compose down
```

`docker compose down`: останавливает и удаляет **оба** sidecar-контейнера этого проекта. Уже выполненные переименования и добавленные сериалы не отменяются; раздачи и медиафайлы не удаляются. Для отката версии используйте сохранённый тег/дайджест образа и прежнюю конфигурацию.

## Резервное копирование

Храните исходники, Compose и несекретную конфигурацию в системе контроля версий. `secrets/normalizer.env` никогда не коммитится и хранится только отдельно/в зашифрованной резервной копии.

## Диагностика

- `already_recognized` — имя уже принято Sonarr, действие не требуется.
- `series_not_resolved` или неоднозначные эпизоды — проверьте каталог, альтернативные названия, год и сезон; пропуск не означает ошибку процесса.
- `sonarr_managed_download` — загрузка уже связана с Sonarr, auto-add её не присваивает.
- Ошибка canonical repair — проверьте полный состав файлов и результат парсера; не переименовывайте чужие файлы ради обхода проверки.
- `healthy` не доказывает импорт. Сверьте завершённые циклы, audit и очередь Sonarr.


MIT — см. [LICENSE](LICENSE).

## Для разработчиков: необязательная сборка из исходников

Для разработки с Git и Docker соберите локальный образ и задайте `image` в локальной копии Compose как `torrent-name-normalizer:dev` в обоих сервисах. Требования к конфигурации, секретам и сети из предыдущих разделов действуют и при разработке.

```sh
git clone https://github.com/vladido/torrent-name-normalizer.git
cd torrent-name-normalizer
docker build -t torrent-name-normalizer:dev .
```
