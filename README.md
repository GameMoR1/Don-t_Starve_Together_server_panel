# DST Panel

Веб-панель для управления выделенным сервером **Don't Starve Together** (кластер Master + Caves): запуск, конфигурация, моды, бэкапы, библиотека миров и статистика игроков.

## Возможности

- **Кластер Master + Caves** — запуск, остановка и перезапуск шардов из браузера
- **Мастер запуска** — пошаговая настройка, Cluster Token, пресеты «для друзей» / «онлайн»
- **Конфигурация** — `cluster.ini`, `server.ini`, моды, списки админов/банов/whitelist
- **Библиотека миров** — несколько именованных сейвов, переключение перед стартом кластера
- **Пересборка мира** — очистка сейва с отслеживанием прогресса
- **Бэкапы** — создание, восстановление, загрузка архивов (конфиг, сейвы, база панели, библиотека миров)
- **Игроки** — учёт сессий по логам DST, время в игре, график активности
- **Безопасность** — роли пользователей, 2FA, аудит действий
- **Метрики** — CPU, RAM, диск; статус шардов в реальном времени

Подробный гайд по работе с интерфейсом: **[docs/guide.md](docs/guide.md)**

## Требования

- **ОС:** Ubuntu / Debian (amd64), root-доступ для установки
- **Ресурсы:** от 2 ГБ RAM (рекомендуется 4+ ГБ для кластера с пещерами)
- **Сеть:** открытые порты DST (по умолчанию Master `10888`, Caves `10889`) и порт панели (`8000`)
- **Зависимости:** устанавливаются скриптом (`python3`, `steamcmd`, i386-библиотеки для DST)

## Быстрый старт

```bash
# Клонировать репозиторий на сервер
git clone <url-репозитория> /opt/dst-panel
cd /opt/dst-panel

# Установка (SteamCMD, DST, systemd, venv)
sudo bash scripts/install.sh
```

После установки панель доступна по адресу `http://<IP-сервера>:8000/`.

| Параметр | Значение по умолчанию |
|----------|------------------------|
| Логин | `admin` |
| Пароль | `admin123` |
| Пользователь DST | `dstpanel` |
| Каталог DST | `/home/dstpanel/dst` |
| Каталог панели | `/opt/dst-panel` |
| Данные панели | `/var/lib/dst-panel/` |

> **Сразу смените пароль** администратора в разделе «Настройки» и при необходимости включите 2FA.

### Firewall

```bash
sudo ufw allow 8000/tcp    # панель
sudo ufw allow 10999/udp   # DST (уточните порты в cluster.ini)
```

## Обновление

```bash
cd /opt/dst-panel
git pull
sudo bash scripts/install.sh          # только панель
sudo UPDATE_DST=1 bash scripts/install.sh   # панель + файлы DST через SteamCMD
```

Перезапуск панели **не останавливает** запущенные шарды DST (`KillMode=process` в systemd).

## Конфигурация окружения

Файл `.env` создаётся при установке. Шаблон — [.env.example](.env.example).

| Переменная | Описание |
|------------|----------|
| `PANEL_HOST` | Адрес привязки (обычно `0.0.0.0`) |
| `PANEL_PORT` | Порт веб-панели |
| `PANEL_SECRET_KEY` | Секрет сессий (генерируется при install) |
| `DST_DIR` | Корень установки DST |
| `DATABASE_PATH` | SQLite-база панели |
| `STEAM_WEB_API_KEY` | Опционально: названия модов и импорт коллекций Steam |

## Структура проекта

```
app/              # FastAPI-приложение, API, сервисы, UI
scripts/          # install.sh, update.sh, backup.sh, run_panel.sh
systemd/          # unit-файлы dst-panel, dst-master, dst-caves
docs/             # документация
tests/            # тесты
```

## Сервисы systemd

```bash
sudo systemctl status dst-panel    # веб-панель
sudo journalctl -u dst-panel -f    # логи панели
```

Шарды DST запускаются панелью по запросу, а не отдельными `dst-master` / `dst-caves` unit-ами в обычном режиме.

## Бэкапы по расписанию

```bash
# Ручной бэкап
sudo bash scripts/backup.sh

# Cron (пример: каждый день в 04:00)
0 4 * * * root /opt/dst-panel/scripts/backup.sh >> /var/log/dst-panel-backup.log 2>&1
```

Архивы хранятся в `/var/lib/dst-panel/backups/`.

## Безопасность

- Не коммитьте `.env`, `cluster_token.txt`, базы данных и бэкапы — они в [.gitignore](.gitignore)
- Cluster Token хранится только на сервере в `DoNotStarveTogether/cluster/cluster_token.txt`
- Для LAN без публикации в списке Klei можно включить **офлайн-кластер** в `cluster.ini`
- Ограничьте доступ к порту панели firewall / VPN

## Разработка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполните локально
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```bash
pytest
```

## Лицензия

Проект для личного / некоммерческого использования. Don't Starve Together — торговая марка Klei Entertainment.
