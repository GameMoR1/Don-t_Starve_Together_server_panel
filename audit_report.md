# Отчёт аудита безопасности и качества кода

Проанализировано 28 файлов в `app/`, `tests/`, `scripts/`, `config/`. Обнаружено **34 замечания**: 3 CRITICAL, 7 HIGH, 11 MEDIUM, 13 LOW. Основные проблемы: отсутствие защиты TOTP-секретов, потенциальные path traversal-векторы, недостаточный rate limit, неатомарные файловые операции, хардкод путей, пробелы в тестовом покрытии.

---

## Находки

### 🔴 CRITICAL

| # | Файл | Строка | Проблема | Рекомендация |
|---|------|--------|----------|--------------|
| 1 | `app/models/models.py` | 14 | TOTP-секрет (`totp_secret`) хранится в SQLite в открытом виде. При компрометации БД злоумышленник получает все 2FA-ключи. | Хранить `totp_secret` зашифрованным (Fernet с мастер-ключом из `PANEL_SECRET_KEY`) или в dedicated HSM-подобном хранилище. |
| 2 | `app/security/auth.py` | 21-24 | `PANEL_SECRET_KEY` по умолчанию — пустая строка. При отсутствии ключа сессии создаются без криптографической подписи (unsigned UUID). | Валидировать наличие `PANEL_SECRET_KEY` при старте панели; генерировать ключ автоматически при первом запуске и сохранять в `.env`. |
| 3 | `app/backup/backup_manager.py` | 71-74 | `restore_backup`: `shutil.rmtree(CLUSTER_DIR)` удаляет данные ДО `tar.extractall`. При сбое между ними данные кластера безвозвратно теряются. | Извлекать во временную директорию, затем атомарно заменять через `os.replace` (если на одной ФС) или rsync. |

### 🟠 HIGH

| # | Файл | Строка | Проблема | Рекомендация |
|---|------|--------|----------|--------------|
| 4 | `app/main.py` | 31 | Пароль администратора выводится в stdout при первом запуске: `print(f"[WARN] Default admin password: {admin_password}")`. | Писать пароль только в отдельный защищённый файл (`/var/lib/dst-panel/admin_credentials.txt` с chmod 600) и удалять после первой смены пароля. |
| 5 | `app/api/auth.py` | 55-92 | Отсутствие rate limiting на `/api/auth/login`. Lockout (5 попыток за 15 мин) не защищает от распределённого brute force (разные IP). | Добавить middleware rate limiting (aiolimiter / slowapi) или как минимум увеличить время блокировки экспоненциально. |
| 6 | `app/api/config_api.py` | 280-287, 290-298 | Path traversal через `/api/config/file/{filename:path}` — filename подставляется в `f"{CLUSTER_DIR}/{filename}"`; хотя `resolve_safe_path` использует `os.path.realpath`, злоумышленник с ролью `admin` может читать/писать файлы вне CLUSTER_DIR через symlink-атаку. | Усилить `resolve_safe_path`: проверять, что resolved path начинается с __реального__ `CLUSTER_DIR` и не содержит обходных symlink-цепочек; добавить allowlist допустимых расширений. |
| 7 | `app/security/auth.py` | 118-127 | Сессия не привязана к IP пользователя; при компрометации session_id (XSS/перехват cookie) злоумышленник может использовать её с другого IP. | Добавить проверку IP-адреса при валидации сессии (с опцией отключения для прокси). |
| 8 | `app/api/auth.py` | 79 | Кука session_id устанавливается с `secure=request.url.scheme == "https"`. Если панель работает через HTTP за reverse proxy, флаг Secure не выставляется. | Всегда устанавливать Secure=true, если приложение работает через reverse proxy с TLS-терминацией (определять по `X-Forwarded-Proto`). |
| 9 | `app/services/world_library.py` | 270-288 | `_apply_dir_to_shard`: `_clear_shard_save` (rmtree/remove save data) затем `shutil.copytree` — при сбое между ними шард остаётся без данных. | Использовать temp-директорию, затем атомарно заменить целевую директорию. |
| 10 | `app/backup/backup_manager.py` | 31 | `create_backup`: `tarfile.open(dest, "w:gz")` пишет напрямую в конечный путь. При сбое остаётся повреждённый файл. | Использовать временный файл + `os.replace` для атомарного сохранения. |

### 🟡 MEDIUM

| # | Файл | Строка | Проблема | Рекомендация |
|---|------|--------|----------|--------------|
| 11 | `app/security/auth.py` | 59-76 | При отсутствии `_serializer` (PANEL_SECRET_KEY не задан) сессия создаётся с UUID и `expires_at`, но любой может подделать session_id с большим будущим `expires_at`. | Никогда не разрешать unsigned-сессии; генерировать ключ при старте, если отсутствует. |
| 12 | `app/api/backups.py` | 82-85 | Проверка загружаемого бэкапа только по расширению (`.tar.gz`/`.tgz`), не валидируется содержимое (нет magic byte check, нет проверки целостности). | Проверять magic-байты gzip/tar; ограничить размер на уровне Uvicorn/Nginx до применения бизнес-логики. |
| 13 | `app/api/auth.py` | 168-186 | Смена пароля не проверяет, что новый пароль отличается от старого. Возможна "смена" на тот же пароль (бессмысленный аудит-лог). | Добавить проверку `not verify_password(req.new_password, user.password_hash)`. |
| 14 | `app/services/dst_service.py` | 570-593 | Ожидание связи Caves → Master — последовательный `sleep(1)` в цикле без возможности отмены при выключении панели. | Использовать `asyncio.wait_for` с `asyncio.Event` для graceful cancellation. |
| 15 | `app/api/config_api.py` | 280-287 | Чтение произвольных текстовых файлов через `/api/config/file/{path}` доступно любому аутентифицированному пользователю (нет ролевой проверки на GET, только на PUT). | Добавить `check_role(user, "admin")` также на GET-эндпоинт. |
| 16 | `app/services/player_service.py` | 109 | `f.readlines()` читает весь файл лога в память; при логах > 500 МБ может вызвать OOM. | Использовать итеративный чтение с конца файла (seek + read backwards) или mmap. |
| 17 | `app/services/dst_service.py` | 1134-1141 | `_archive_path`: `os.remove` + `shutil.move` без временной директории. При сбое между ними данные могут быть потеряны. | Использовать temp-директорию + `os.replace`. |
| 18 | `app/config/config_reader.py` | 156-157, 459-460, 492-493, 553-554 | Множественные прямые записи в конфиги (`write_cluster_ini`, `write_shard_ini`, `write_text_file`, `write_cluster_token`) без временного файла. При сбое возможны truncated/corrupt файлы. | Использовать временный файл + `os.replace` для атомарной записи. |
| 19 | `app/main.py` | 50 | В lifespan context manager отсутствует код очистки после `yield`. Нет отмены `_regen_task` при остановке панели. | Добавить отмену `_regen_task` и завершение процессов шардов в shutdown-фазе. |
| 20 | `app/services/dst_service.py` | 1451 | `_regen_task = asyncio.create_task(...)` — задача регенерации мира никогда не отменяется при выключении. Нет обработки `asyncio.CancelledError`. | Отменять `_regen_task` в shutdown-фазе; добавить `try/except asyncio.CancelledError` в `_regenerate_world_worker`. |
| 21 | `app/api/metrics.py` | 16-35 | `psutil` — необязательная зависимость в `requirements.txt`, но импорт сделан внутри функции; при отсутствии возвращается `{"error": "psutil not available"}`. | Вынести psutil в секцию `extras` requirements.txt или сделать обязательной зависимостью. |

### 🔵 LOW

| # | Файл | Строка | Проблема | Рекомендация |
|---|------|--------|----------|--------------|
| 22 | `app/main.py` | 67-72 | `TrustedHostMiddleware` применяется только если задан `PANEL_ALLOWED_HOSTS`. По умолчанию — нет защиты заголовка Host. | Включить `TrustedHostMiddleware` с `allowed_hosts=["*"]` как fallback (или генерировать значение по умолчанию из PANEL_HOST). |
| 23 | `.env.example` | 7 | `PANEL_SECRET_KEY=openssl rand -hex 32` — строка является командой, а не значением. Если скопировать буквально в `.env`, ключ будет текстом "openssl rand -hex 32". | Заменить на комментарий с инструкцией: `# PANEL_SECRET_KEY=$(openssl rand -hex 32)`. |
| 24 | `app/api/audit.py` | 20-21 | В audit/logs нет пагинации для общего количества записей; `total` равен длине текущей страницы, а не общему кол-ву записей. | Использовать `select(func.count()).select_from(AuditLog)` для получения реального total. |
| 25 | `tests/test_auth.py` | 1-18 | Всего 2 теста (хэширование пароля + валидация). Нет тестов для: login/logout, rate limiting, TOTP, audit, config, server, backups. | Добавить интеграционные тесты хотя бы для критических API-эндпоинтов (auth, config, server). |
| 26 | `scripts/backup.sh` | 83-84 | Ротация бэкапов по маске `dst_*.tar.gz`, но панель создаёт файлы с маской `backup_*.tar.gz` — ротация никогда не сработает. | Изменить маску поиска на `backup_*.tar.gz` или `*.tar.gz`. |
| 27 | `app/main.py` | 59-72 | Отсутствуют заголовки безопасности HTTP-ответов: CSP (`Content-Security-Policy`), `X-Content-Type-Options: nosniff`, `X-Frame-Options`/`frame-ancestors`, HSTS. | Добавить middleware, устанавливающий security-заголовки (CSP, nosniff, frame-ancestors, HSTS) для всех ответов. |
| 28 | `app/services/dst_service.py` | 33 | `BACKUP_DIR = "/var/lib/dst-panel/backups"` — хардкод пути, не вынесен в .env. | Вынести базовый путь в конфигурацию через переменную окружения. |
| 29 | `app/services/dst_service.py` | 34 | `PANEL_LOG_DIR = "/var/lib/dst-panel/shard-logs"` — хардкод пути, не вынесен в .env. | Вынести в конфигурацию через переменную окружения. |
| 30 | `app/services/shard_registry.py` | 22 | `REGISTRY_PATH = "/var/lib/dst-panel/shard-pids.json"` — хардкод пути, не вынесен в .env. | Вынести в конфигурацию через переменную окружения. |
| 31 | `app/services/world_library.py` | 23-26 | `WORLD_LIBRARY_DIR`, `WORLDS_DIR`, `REGISTRY_PATH`, `ACTIVE_PATH` — хардкод в `/var/lib/dst-panel/world-library`. | Вынести базовый путь в конфигурацию (.env). |
| 32 | `app/services/systemd_service.py` | 4 | `SYSTEMD_DIR = "/etc/systemd/system"` — хардкод пути. | Вынести в конфигурацию (.env). |
| 33 | `app/config/config_reader.py` | 10 | `DST_DIR = os.environ.get("DST_DIR", "/home/dstpanel/dst")` — fallback путь хардкодом. | Заменить на комментарий с инструкцией по установке; не использовать fallback. |
| 34 | `app/config/config_reader.py` | 29 | `TOKEN_BACKUP_PATH = "/var/lib/dst-panel/.cluster_token.backup"` — хардкод пути, не вынесен в .env. | Вынести в конфигурацию через переменную окружения. |

---

*Аудит проведён на основе кода в директории `/data/projects/510fe511-ec2a-4d69-aa7e-6c97e4a75f71/`. Все severity-оценки даны исходя из OWASP Top 10 (2021) и лучших практик Python/FastAPI.*