Этапы строго последовательны: 1 → 2 → 3 → 4 → 5 → 6. `open_box` меняют этапы 2 и 3, а формат id и форма ответов из этапов 3–4 нужны этапу 5. Каждый этап заканчивается зелёными тестами и работающим сервером. Ссылки: `[Rn]` — `REVIEW-1.md`, `[Sn]` — `REVIEW-2.md`, `Dn` — `design.md`.

## 1. Окружение и базовая линия

- [x] 1.1 Установить `uv` (с подтверждением пользователя). Проверка: `uv --version`.
- [x] 1.2 На нетронутом upstream выполнить `uv sync --locked`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`. Проверка: 31 тест зелёный, ruff без ошибок. Это базовая линия.
- [x] 1.3 `uv add keyring pypdf cryptography defusedxml`, затем `uv lock`. Проверка: `uv sync --locked` проходит, тесты зелёные.
- [x] 1.4 Обновить `pyproject.toml` (`authors`, `urls`, описание), добавить строку о форке в `LICENSE`.
- [x] 1.5 CI: матрица `ubuntu-latest`, `windows-latest` × Python 3.12, 3.13, 3.14 (D20). Проверка: `uv run --python 3.14 pytest` локально зелёный.

## 2. Гарантия «только чтение» в клиенте (readonly-imap-access)

- [x] 2.1 `guard.py` — `ReadOnlyIMAP4_SSL`:
  - allowlist команд и подкоманд `UID`;
  - запрет `\r`, `\n`, `\0` во всех аргументах;
  - callable-литерал допустим только для `AUTHENTICATE` `[S1]`;
  - токенизатор элементов `FETCH`;
  - fail-closed;
  - `ReadOnlyMailBox` с `timeout=30`.
- [x] 2.2 `open_box`: вход с `initial_folder=None`, затем `folder.set(folder, readonly=True)`.
- [x] 2.3 Тесты на фейковом сокете, все вызовы напрямую через клиент, в обход проверки аргументов `[S10]`:
  - уходит `EXAMINE`, `SELECT` не уходит;
  - отклоняются до записи в сокет: `SELECT`, `STORE`, `UID STORE`, `APPEND`, `COPY`, `MOVE`, `EXPUNGE`, `CLOSE`, `CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE`;
  - `UID FETCH ... BODY.PEEK[HEADER]` проходит, а `BODY[]` и `RFC822` отклоняются;
  - `client.uid("SEARCH", ..., "x\r\nZ1 DELETE \"Проекты\"")` отклонён, в сокете ноль байт этой команды;
  - `client.select("INBOX\nZ2 DELETE INBOX", readonly=True)` отклонён;
  - callable-литерал у `SEARCH` отклонён;
  - после отклонённого `append` литерал не уходит, соединение закрыто;
  - `box.flag()`, `box.delete()`, `box.move()`, `box.append()`, `box.folder.create()` дают `ReadOnlyViolation`;
  - `timeout` передаётся.
- [x] 2.4 Тест-канарейка: сигнатура `imaplib.IMAP4._command`; `select`, `uid`, `append`, `store`, `copy` вызывают `_command`; `MailBox._get_mailbox_client` существует.

## 3. Способы входа и OAuth Яндекса (account-auth)

- [x] 3.1 Проверить на теге mcp `v2.0.0`, проходит ли `CallToolResult(isError=True)` из инструмента с аннотацией объекта мимо проверки схемы выхода `[S2]`. Тест на реальном `MCPServer` без сети. Результат и выбранный способ — в `RUN.md`.
- [x] 3.2 `accounts.py`:
  - поля `auth`, `oauth_provider`, `client_id`, формат `key`;
  - дефолты из профиля;
  - валидация по аккаунту со статусом `config-error`;
  - перечитывание по mtime;
  - отсутствующий файл не роняет процесс.

  Тесты на каждый сценарий спеки.
- [x] 3.3 `accounts.example.toml`: пример Яндекс-аккаунта с `auth = "oauth"`; поправить `test_example_config_is_loadable` `[R16]`.
- [x] 3.4 `auth/base.py`: `CredentialProvider` (`offline_status`, `login`, `fingerprint`, `secrets`); `PasswordCredential` с логикой upstream. Тесты входа по паролю зелёные.
- [x] 3.5 `auth/store.py`:
  - keyring: сервис `imap-mcp`, JSON на аккаунт, `client_secret` отдельно;
  - `persist = "local_machine"` для `WinVaultKeyring`;
  - лимит 1280 символов;
  - ошибка недоступного бэкенда.

  Тесты на in-memory backend и тест, что `persist` выставляется только для Windows-бэкенда.
- [x] 3.6 `auth/profiles.py`: `ProviderProfile` (D1) и профиль `yandex`; фикстуры — дословно из документации Яндекса.
- [x] 3.7 `auth/http.py` на `urllib.request`: `timeout=15`, классификация ошибок (`HTTPError` раньше `URLError`), подменяемый opener `[S4]`, `[S12]`. Тесты: 400 с `invalid_grant`, 503, `TimeoutError`, ответ без JSON.
- [x] 3.8 `auth/oauth.py`:
  - device flow с `code`, `grant_type=device_code`, `device_id`, `device_name`;
  - опрос по `interval`;
  - фатальные коды с текстами, `access_denied`, `expired_token`, `slow_down`, неизвестный код как фатальный;
  - проверка `scope`;
  - пробный вход в IMAP перед сохранением;
  - Ctrl+C → код 130;
  - `offline_status()` по `expires_at`: `reauth-required` после истечения, предупреждение за 30 дней;
  - отказ входа при действующем токене → `reauth-required` с двумя причинами.

  Тесты на каждый сценарий спеки.
- [x] 3.9 Запасной путь однострочного `AUTHENTICATE XOAUTH2 <b64>` с `state = "AUTH"`. Тест: после него `EXAMINE` проходит `[S11]`.
- [x] 3.10 `redact()` и `@safe_tool` (D15). Тесты:
  - XOAUTH2-base64 и access-токен в исключении заменены на `***`;
  - перебор всех зарегистрированных инструментов: исключение с секретом даёт `isError: true` и отредактированный текст.
- [x] 3.11 `cli.py`: без аргументов — `serve`; `auth [--trace]`, `status [--offline]`, `forget`. Тесты:
  - без аргументов вызывается `mcp.run`;
  - `auth` для парольного аккаунта не делает сетевых запросов;
  - `status --offline` не открывает сокетов;
  - `--trace` не печатает секретов;
  - `forget` с `client_secret` вызывает отзыв, без него печатает инструкцию; повтор — «нечего удалять».
- [x] 3.12 `list_accounts` возвращает `{accounts, warnings}`: поле `auth`, реальный вход для `ok`, дата истечения, предупреждение, «нет включённых аккаунтов». Тест обновлён.

## 4. Инструменты для анализа почты (mail-tools)

- [ ] 4.1 `validate.py` (D7). Тесты на `id="1:*"`, `query` с CR/LF, границы, неверный `since` — без обращения к IMAP.
- [ ] 4.2 `ids.py`: `v1.<key>.<b64url>.<uidvalidity>.<uid>`, разбор, сверка UIDVALIDITY (D8). Тесты:
  - кодирование туда и обратно для кириллической папки и папки с `/`;
  - отказ на мусор;
  - «список устарел»;
  - неизвестный или выключенный аккаунт.
- [ ] 4.3 `ConnectionPool` (D13):
  - `pool_lock` и `account_lock` с фиксированным порядком;
  - отпечаток аккаунта;
  - `NOOP`;
  - закрытие после 2 минут простоя;
  - дедлайн 45 с;
  - без переподключения после `TimeoutError`.

  Тесты:
  - 20 чтений — один вход;
  - смена отпечатка (правка конфига, `forget`) закрывает соединение;
  - непригодное соединение не возвращается в пул;
  - два потока на один аккаунт без взаимной блокировки;
  - медленный фейк-сервер даёт `unreachable: timeout` не позже 45 с по фейковым часам.
- [ ] 4.4 Форма ответов `{rows, errors, limit_capped, truncated}` с типизированной аннотацией; бюджет 45 000 символов `[S2]`, `[S16]`.
- [ ] 4.5 `list_folders`: modified UTF-7, special-use, `selectable`, `STATUS` только для выбираемых, `status_error`. Тесты: кириллическое имя, `\Noselect`.
- [ ] 4.6 `folder` и псевдонимы special-use в `list_emails` и `search_emails`; ошибка папки — в `errors`; пустая папка. Тест `\Junk` на два аккаунта.
- [ ] 4.7 Два прохода по `size_rfc822`: ≤ 128 КБ, бюджет 3 МБ, `limit` ≤ 100, `limit_capped`, `size` (D10). Тест на фейке с различающимися `size` и `size_rfc822`.
- [ ] 4.8 `search.py`: три `UID SEARCH CHARSET UTF-8 ... {n}` для не-ASCII с объединением UID и ASCII-путь `imap-tools` (D9). Тесты по байтам в сокете для `"счёт"` (три команды, литерал в конце каждой) и для `"invoice"` (одна команда).
- [ ] 4.9 `htmltext.py` (D11). Тесты:
  - `<style>` и таблица;
  - сущности;
  - `display:none`, `hidden`, `font-size:0`, `mso-hide:all`;
  - незакрытый скрытый `<p>`;
  - предохранитель «больше половины удалено»;
  - ссылки.
- [ ] 4.10 `get_email(id, offset, max_chars ≤ 40 000)`: `limit=1`, поля спеки, `next_offset`, порог 40 МБ. Тесты на постраничное чтение и на большое письмо.
- [ ] 4.11 Даты: 1900-01-01 → `null`, дата без зоны считается UTC, сортировка по UTC, `null` в конце `[S18]`. Тесты.
- [ ] 4.12 Изоляция ошибок: разрешение аккаунтов внутри обработки ошибок. Тест: один аккаунт бросает `TimeoutError`, второй отвечает.
- [ ] 4.13 Предупреждение о недоверенном содержимом в описаниях инструментов.

## 5. Вложения (attachment-text)

- [ ] 5.1 Метаданные `attachments` в `get_email`, `extractable` по сигнатуре и размеру. Тест: PDF и встроенный PNG.
- [ ] 5.2 `attachments.py`:
  - тип по сигнатуре (`%PDF-` в первых 1024 байтах, ZIP с `word/document.xml`, BOM, затем правило нулевых байтов);
  - общие MIME игнорируются;
  - `type mismatch`.
- [ ] 5.3 `_extract_worker.py` без импорта `server`, `mcp` и IMAP-слоя:
  - перенаправление stdout и stderr (и дескрипторов 1, 2) в `devnull`;
  - TXT с BOM, charset, fallback UTF-8 → cp1251;
  - DOCX через `zipfile` + `defusedxml` со счётчиком прочитанных байтов;
  - PDF через `pypdf`: 50 страниц, `decrypt("")`, `owner-password protected` / `encrypted`, `no text layer`.
- [ ] 5.4 Запуск исполнителя (D12):
  - `spawn`-процесс на вызов, `Pipe` с сигналом «начинай»;
  - Job Object с лимитом 1 ГБ на Windows, `RLIMIT_AS` 2 ГБ на Linux, на macOS без лимита памяти;
  - `join(30)` → `kill()`;
  - лимит 15 МБ до запуска.
- [ ] 5.5 Кэш двух последних писем, TTL 5 минут, без писем больше 25 МБ.
- [ ] 5.6 Инструмент `get_attachment_text(id, index, offset, max_chars ≤ 40 000)`. Тест `test_all_tools_registered` проверяет шесть инструментов.
- [ ] 5.7 Фикстуры и тесты по каждому сценарию спеки:
  - PDF с текстом; PDF как `octet-stream`; смещённый `%PDF-`; `image/png` с содержимым PDF;
  - скан без текстового слоя; PDF с паролем владельца (AES); PDF с паролем пользователя;
  - DOCX с таблицей; DOCX с XXE; ZIP-бомба 2 МБ → 2 ГБ;
  - TXT в cp1251; CSV в UTF-16 LE с BOM;
  - неверный индекс и письмо без вложений; неподдерживаемый тип;
  - три вложения — одно скачивание;
  - таймаут: тестовый исполнитель засыпает, после `timeout` следующий вызов выполняется сразу и живых дочерних процессов нет;
  - лимит памяти: тестовый исполнитель выделяет больше лимита, результат — `memory limit` (на macOS тест пропускается);
  - вывод исполнителя через `print` не попадает в stdout родителя.

## 6. Живая проверка, рабочее место и документация

- [ ] 6.1 `probe.py` и `verify-readonly` (D16):
  - ленивый импорт;
  - пробы `CREATE` и `UID STORE +FLAGS.SILENT (\Seen)`;
  - `PERMANENTFLAGS`;
  - `NO` на `SELECT` считается отказом;
  - откат в `finally`;
  - поиск остатков;
  - коды 0/1/2 и перечень проверенного и непроверенного;
  - `--trace`;
  - подтверждение без `--yes`.

  Тесты на фейковом сервере по каждому сценарию спеки. Тест: после старта в режиме сервера `imap_mcp.probe` нет в `sys.modules`.
- [ ] 6.2 **Вручную, пользователь:**
  1. зарегистрировать приложение на oauth.yandex.ru только с правом «Доступ на чтение писем в почтовом ящике»;
  2. включить «IMAP с OAuth-токенами» (для Яндекс 360 — уточнить у администратора);
  3. выполнить `imap-mcp auth yandex --trace`.

  В `RUN.md` записать: нужен ли `client_secret`, `expires_in`, форму `AUTHENTICATE` по трассировке. Норма: код 0, `imap-mcp status` (с реальным входом) показывает `ok`.
- [ ] 6.3 **Вручную, пользователь:** `imap-mcp verify-readonly yandex --trace`. Норма: код 0. Ответы сервера и перечень проверенного записать в `RUN.md`. При коде 1 — запись в README о том, какие операции защищает только код.
- [ ] 6.4 Шаблон `examples/claude-mail-workspace/` (D19):
  - `dontAsk` + allow `mcp__imap-mcp__*` + deny вторым слоем;
  - `disableClaudeAiConnectors`;
  - `.mcp.json` с `timeout`;
  - `CLAUDE.md`.
- [ ] 6.5 **Вручную, пользователь:** открыть шаблонную папку в Claude Code (расширение VS Code) и проверить:
  - панель MCP или `/mcp` показывает только `imap-mcp` — иначе этап не готов;
  - «покажи 5 последних писем», «найди письма со словом счёт», «прочитай текст PDF-вложения», «покажи спам» — ответы есть, а русский поиск находит письма (проверка литерала на Яндексе);
  - «открой ссылку из письма», «выполни команду в терминале», «сохрани сводку в файл», «прочитай файл .env» — вызовы отклонены без запроса подтверждения.

  Результат записать в `RUN.md`.
- [ ] 6.6 `docs/yandex-setup.ru.md`:
  - регистрация приложения;
  - IMAP с OAuth для личного ящика;
  - раздел для Яндекс 360: что проверить у администратора;
  - `auth`, `status`, `verify-readonly`, `forget`;
  - ежегодная повторная авторизация;
  - подключение к Claude Code;
  - что делать при `reauth-required`.

  В README — раздел о способах входа, вложениях и остаточных рисках (искажённые выводы, «белый на белом», непроверенные `APPEND` и `COPY`).
- [ ] 6.7 Финальная проверка: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` зелёные на 3.12 и 3.14; `openspec validate add-yandex-oauth-readonly --strict` без ошибок.
