# Conan Exiles Shop Bot

A modern, fully async Discord bot for Conan Exiles dedicated servers.
Built on **discord.py 2.x**, **APScheduler**, **aiomysql**, and **aiosqlite**.

---

## ✨ Features

| Feature | Description |
|---|---|
| **Discord Shop** | Players browse, buy, and check balance via slash commands |
| **Auto Delivery** | Items delivered in-game via RCON; queued for offline players |
| **Payroll** | Online players earn currency on a configurable interval |
| **Player Sync** | Online player list synced from RCON every 5 minutes |
| **Registration** | Players link Discord ↔ Conan account with a one-time code |
| **🧊 Black Ice Converter** | Every 2 min: converts dropped Black Ice → Hardened Bricks (10:1) |
| **Server Buffs** | Timed server-wide buffs purchasable from the shop |
| **Vault Rentals** | Players rent vaults via Discord for a per-day coin fee |
| **Kill Log** | Kill events streamed live to a dedicated Discord channel |
| **Building Leaderboard** | Clan building piece & inventory counts posted every 10 min |
| **Building Tracker** | Clan building piece counts updated every minute |
| **Jail System** | Admin can sentence, teleport, and auto-release players |
| **Admin Commands** | Give currency/items, teleport, jail, broadcast — all slash commands |

---

## 🏗️ Architecture

```
Single asyncio event loop — no multiprocessing, no restart timers.

┌─────────────────────────────────────────────────────┐
│                    bot/main.py                       │
│                                                     │
│  discord.py Bot        APScheduler (async)          │
│  ┌──────────────┐      ┌──────────────────────────┐ │
│  │ cogs/shop    │      │ tasks/payroll      30min  │ │
│  │ cogs/admin   │      │ tasks/usersync      5min  │ │
│  │ cogs/register│      │ tasks/orderprocessing 5s  │ │
│  └──────────────┘      │ tasks/black_ice_conv 2min │ │
│                        │ tasks/game_db_watcher 1min│ │
│                        └──────────────────────────┘ │
│                                                     │
│  tasks/game_log_watcher  (background coroutine)     │
└─────────────────────────────────────────────────────┘
        │                          │
   MariaDB (aiomysql)        game.db (aiosqlite, read-only)
```

---

## 📋 Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) |
| MariaDB 10.6+ | Or use the included Docker Compose setup |
| Conan Exiles dedicated server | With RCON enabled |
| Discord Bot token | [discord.com/developers](https://discord.com/developers/applications) |

---

## 🚀 Quick Start (Local)

### 1 — Clone the repo

```bash
git clone https://github.com/aquesada97/Conan-Exiles-Shop.git
cd Conan-Exiles-Shop
```

### 2 — Create a virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### 4 — Configure the bot

```bash
copy .env.example .env   # Windows
# or
cp .env.example .env     # macOS / Linux
```

Open `.env` in a text editor and fill in **every** value.  
See [Configuration Reference](#-configuration-reference) below for details.

### 5 — Set up the database

Make sure MariaDB is running and you have created an empty database matching `DB_NAME` in your `.env`.

```bash
python setup_db.py
```

This creates all tables automatically.

### 6 — Add items to the shop *(optional)*

Connect to MariaDB and insert rows into `shop_items`:

```sql
INSERT INTO shop_items (itemName, itemDescription, itemPrice, itemid, itemType, category, isActive)
VALUES
  ('Iron Sword',    'A basic sword.',          50,  '51021', 'single',  'Weapons', 1),
  ('Healing Wrap',  'Restores health.',         20,  '18252', 'single',  'Medical', 1),
  ('Starter Kit',   'New player essentials.',  100,  '18040', 'kit',     'Kits',    1);
```

### 7 — Run the bot

```bash
python -m bot.main
```

---

## 🐳 Docker Deployment (Recommended for Production)

### 1 — Set up `.env`

Same as step 4 above. Make sure `GAME_DB_PATH` and `GAME_LOG_PATH` point to the
actual files on your **host** machine (they will be mounted read-only into the container).

### 2 — Start everything

```bash
docker compose up -d
```

This starts:
- **MariaDB** (with persistent volume)
- **Bot** (waits for MariaDB to be healthy before starting)

### 3 — Init the database (first run only)

```bash
docker compose exec bot python setup_db.py
```

### 4 — View logs

```bash
docker compose logs -f bot
```

### Useful Docker commands

```bash
docker compose restart bot          # restart only the bot
docker compose down                 # stop everything
docker compose down -v              # stop + wipe database volume
```

---

## ⚙️ Configuration Reference

All settings live in `.env`. Here are the most important ones:

### MariaDB
| Key | Default | Description |
|---|---|---|
| `DB_HOST` | `127.0.0.1` | MariaDB host |
| `DB_PORT` | `3306` | MariaDB port |
| `DB_USER` | *(required)* | Database user |
| `DB_PASS` | *(required)* | Database password |
| `DB_NAME` | *(required)* | Database name |

### Conan Server
| Key | Default | Description |
|---|---|---|
| `SERVER_NAME` | `ConanExiles` | Prefix for per-server tables. Must be alphanumeric. |
| `RCON_HOST` | `127.0.0.1` | RCON host |
| `RCON_PORT` | `25575` | RCON port |
| `RCON_PASS` | *(required)* | RCON password |
| `GAME_DB_PATH` | *(required)* | Full path to `game.db` |
| `GAME_LOG_PATH` | *(required)* | Full path to `ConanSandbox.log` |

### Shop
| Key | Default | Description |
|---|---|---|
| `STARTING_CASH` | `100` | Currency given to new players |
| `PAYCHECK` | `50` | Currency earned each interval |
| `PAYCHECK_INTERVAL_MINUTES` | `30` | How often payroll runs |
| `CURRENCY_NAME` | `coins` | Displayed currency name |

### 🧊 Black Ice Converter
| Key | Default | Description |
|---|---|---|
| `BLACK_ICE_ITEM_ID` | `18040` | Conan Exiles template ID for Black Ice |
| `HARDENED_BRICK_ITEM_ID` | `11142` | Conan Exiles template ID for Hardened Brick |
| `BLACK_ICE_CONVERSION_RATE` | `10` | How many Black Ice = 1 Hardened Brick |
| `BLACK_ICE_CHECK_INTERVAL_SECONDS` | `120` | How often the converter runs (seconds) |

### Roles
| Key | Default | Description |
|---|---|---|
| `ADMIN_ROLE` | `Admin` | Discord role name with admin commands |
| `MOD_ROLE` | `Moderator` | Discord role name with mod commands |

---

## 💬 Discord Commands

### Player Commands

| Command | Description |
|---|---|
| `/register` | Get a code to link your Discord account to your Conan character |
| `/balance` | Check your current coin balance |
| `/shop [category]` | Browse available items (optionally filtered by category) |
| `/buy <item_name> [quantity]` | Purchase an item — delivered in-game automatically |
| `/listvaults` | See available vaults and their rental prices |
| `/rentvault <name> <days>` | Rent a vault for 1–30 days (costs coins per day) |
| `/myvaults` | See your active vault rentals and expiry times |
| `/releasevault <name>` | Release a vault early (no refund) |

### Admin Commands *(requires Admin or Moderator role)*

| Command | Description |
|---|---|
| `/givecurrency <user> <amount>` | Add coins to a player's wallet |
| `/giveitem <platform_id> <template_id> <qty>` | Give an in-game item via RCON |
| `/jail <character> <minutes> [reason]` | Teleport player to jail for a set duration |
| `/teleport <character> <x> <y> <z>` | Teleport a player to coordinates |
| `/broadcast <message>` | Send a message to all online players |
| `/processblackice` | Manually trigger the Black Ice → Hardened Brick conversion |

---

## 🧊 Black Ice Converter — How It Works

The converter gives players a way to **trade Black Ice for Hardened Bricks** automatically.

### Flow

```
Player drops Black Ice in-game
          ↓
game_log_watcher detects the drop event in ConanSandbox.log
          ↓
Drop is recorded in {SERVER_NAME}_black_ice_pending (MariaDB)
          ↓
Every 2 minutes: black_ice_converter.py runs
          ↓
For each player with pending drops:
  bricks   = floor(total / BLACK_ICE_CONVERSION_RATE)   ← default: ÷10
  remainder is kept for next cycle
          ↓
Player online?  → RCON spawnitem  (instant delivery)
Player offline? → queued in order_processing (delivered on next login)
```

### Example
A player drops **35 Black Ice** (across multiple drops):
- `35 ÷ 10 = 3` Hardened Bricks delivered immediately
- `5` Black Ice carried over to next cycle

### Adjusting the log detection regex

The converter reads drops from the server log.  
Open `bot/tasks/game_log_watcher.py` and update `RE_BLACK_ICE_DROP`
if your server log format is different from the default:

```python
RE_BLACK_ICE_DROP = re.compile(
    r"(?P<char>.+?)\s+dropped\s+Black\s*Ice\s+(?:amount:|x)(?P<amount>\d+)",
    re.IGNORECASE,
)
```

To find the exact format in your log, search for lines containing `dropped` when
a player drops Black Ice in-game, with `DEBUG` logging enabled.

---

## 🗄️ Database Tables

All per-server tables use your `SERVER_NAME` as a prefix (e.g. `ConanExiles_currentusers`).

| Table | Purpose |
|---|---|
| `accounts` | Player wallets, Discord links, VIP multipliers |
| `servers` | Server RCON/query config |
| `shop_items` | Item catalogue |
| `order_processing` | Pending/completed shop orders |
| `shop_log` | Purchase history |
| `registration_codes` | One-time link codes |
| `{SN}_currentusers` | Who is currently online |
| `{SN}_building_piece_tracking` | Clan building counts |
| `{SN}_inventory_tracking` | Clan container item counts |
| `{SN}_jail_info` | Active jail sentences |
| `{SN}_teleport_requests` | Queued teleport operations |
| `{SN}_homelocations` | Player home coordinates |
| `{SN}_black_ice_pending` | Black Ice pending conversion |

---

## 🔍 Logs

Logs are written to `logs/bot.log` (rotated at 10 MB, last 5 files kept)
and also streamed to the console at `INFO` level.

To enable verbose `DEBUG` output, edit `bot/main.py` and change the console
`level` from `"INFO"` to `"DEBUG"`.

---

## 🛠️ Troubleshooting

**Bot starts but slash commands don't appear**  
Discord can take up to 1 hour to propagate global slash commands.
For instant testing, sync to a specific guild by replacing `await bot.tree.sync()` with
`await bot.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))` in `bot/main.py`.

**RCON connection refused**  
- Confirm `RCON_HOST`, `RCON_PORT`, and `RCON_PASS` in `.env`
- Check that the Conan server has RCON enabled (`RconEnabled=True` in `Engine.ini`)
- Test manually: `rcon -H 127.0.0.1 -P 25575 -p yourpass listplayers`

**Black Ice drops not being detected**  
- Ensure `GAME_LOG_PATH` points to the live log file (not a copy)
- Drop some Black Ice in-game, then search `logs/bot.log` for the word `dropped`
- If nothing appears, update `RE_BLACK_ICE_DROP` in `game_log_watcher.py`

**`game.db` errors / read failures**  
- The bot opens `game.db` as read-only; ensure the path in `GAME_DB_PATH` is correct
- On Windows, use double backslashes or forward slashes:  
  `GAME_DB_PATH=C:/Conan/ConanSandbox/Saved/game.db`

---

## 📦 Tech Stack

| Library | Version | Use |
|---|---|---|
| [discord.py](https://discordpy.readthedocs.io/) | 2.x | Discord bot, slash commands, Cogs |
| [APScheduler](https://apscheduler.readthedocs.io/) | 3.x | Async task scheduling |
| [aiomysql](https://aiomysql.readthedocs.io/) | 0.2+ | Async MariaDB connection pool |
| [aiosqlite](https://aiosqlite.omnilib.dev/) | 0.19+ | Async read access to game.db |
| [rcon](https://pypi.org/project/rcon/) | 2.x | RCON protocol client |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | 2.x | Config validation from .env |
| [loguru](https://loguru.readthedocs.io/) | 0.7+ | Structured logging with rotation |
| [aiofiles](https://github.com/Tinche/aiofiles) | 23+ | Async log file tailing |

---

## 📄 License

Based on work from [irrelevantgamers/Conan-Shop](https://github.com/irrelevantgamers/Conan-Shop)
and [irrelevantgamers/IG_Bot](https://github.com/irrelevantgamers/IG_Bot), licensed under GPL-3.0.  
This project is also released under **GPL-3.0**.
