# Systemd: запуск веб-стенда

Инструкции для Linux/VDS. Публичный VDS пока не обновляется — это
подготовка к следующему деплою.

## 1. Размещение и зависимости

```bash
sudo mkdir -p /opt/smart-search-hackathon
sudo rsync -a --exclude .git ./ /opt/smart-search-hackathon/
cd /opt/smart-search-hackathon

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Индекс каталога `data/organizer_index.sqlite` собирается заранее:

```bash
.venv/bin/python organizer_index.py build
.venv/bin/python organizer_index.py stats
```

## 2. Сертификат НУЦ Минцифры

Проверку TLS не отключаем. Скачайте корневой сертификат и положите его
в системное хранилище либо укажите путь в `GIGACHAT_CA_BUNDLE_FILE`.

```bash
sudo curl -fsSL "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt" \
  -o /etc/ssl/certs/russian_trusted_root_ca_pem.crt
sudo update-ca-certificates
```

## 3. Переменные окружения

Секреты хранятся в `/etc/smart-search-hackathon.env` (не в Git):

```bash
sudo install -m 600 -o root -g root /dev/null /etc/smart-search-hackathon.env
sudo tee /etc/smart-search-hackathon.env >/dev/null <<'EOF'
AI_PROVIDER=gigachat
GIGACHAT_CREDENTIALS=<ключ авторизации GigaChat>
GIGACHAT_MODEL=GigaChat-2
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_CA_BUNDLE_FILE=/etc/ssl/certs/russian_trusted_root_ca_pem.crt
AI_MAX_CALLS_PER_SESSION=8
AI_TIMEOUT_SECONDS=20
PORT=8000
EOF
sudo chmod 600 /etc/smart-search-hackathon.env
```

`GIGACHAT_CREDENTIALS` — ключ авторизации для физлиц (scope
`GIGACHAT_API_PERS`). Ключ читается только на сервере и не попадает
в браузер.

## 4. Установка юнита

```bash
sudo cp deploy/systemd/smart-search.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart-search
sudo systemctl status smart-search --no-pager
```

Проверка:

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool
```

В ответе `ai_provider` = `gigachat`, `ai_live` = `true`, `ai_model` =
`GigaChat-2`. Если ключа нет, `ai_live` = `false`, а консультант
показывает локальную подборку с пометкой «ответ получен без модели».

## 5. Логи и обновление

```bash
sudo journalctl -u smart-search -f
sudo systemctl restart smart-search
```

## 6. Регрессия перед деплоем

```bash
cd /opt/smart-search-hackathon
.venv/bin/python check_gigachat.py          # заглушка, без ключа
.venv/bin/python check_gigachat.py --live   # один живой прогон
.venv/bin/python check_top4.py
```
