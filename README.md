# Conan Exiles Shop Bot

A fully asynchronous Discord bot for Conan Exiles dedicated servers. Combines an in-game shop, player economy, kill tracking, PVP leaderboards, vault rentals, and server buff management — all controlled through Discord slash commands.

---

## Table of Contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration Reference](#configuration-reference)
5. [Database Setup](#database-setup)
6. [Running the Bot](#running-the-bot)
7. [Docker Deployment](#docker-deployment)
8. [Discord Commands](#discord-commands)
9. [Background Tasks](#background-tasks)
10. [Shop Item Types](#shop-item-types)
11. [Black Ice Converter](#black-ice-converter)
12. [Kill Tracking and Leaderboards](#kill-tracking-and-leaderboards)
13. [Wanted Player System](#wanted-player-system)
14. [Vault Rental System](#vault-rental-system)
15. [Server Buffs](#server-buffs)
16. [Jail System](#jail-system)
17. [Multi-Server Support](#multi-server-support)
18. [Watchdog (Process Supervisor)](#watchdog-process-supervisor)
19. [RCON Health Monitoring](#rcon-health-monitoring)
20. [Server Settings Watcher](#server-settings-watcher)
21. [Firewall Blocklist](#firewall-blocklist)
22. [Adjusting Log Regexes](#adjusting-log-regexes)
23. [Troubleshooting](#troubleshooting)
24. [Tech Stack](#tech-stack)

---

## Features

- In-game shop with slash commands — browse categories, buy items, check balance
- Automatic RCON item delivery; orders are queued and retried for offline players
- Periodic payroll — online players earn currency on a configurable interval
- Player account registration linking Discord to a Conan character
- Black Ice to Hardened Brick conversion (10:1, runs every 2 minutes)
- Kill logging streamed to a dedicated Discord channel
- Solo and clan PVP leaderboards across four time windows (1 day, 7 days, 30 days, all time)
- Wanted player system with kill streaks, bounties, and automatic level degradation
- Vault rental system with per-day pricing and automatic expiration
- Timed server buffs purchasable from the shop
- Building piece and inventory leaderboards updated every 1 minute
- Jail system with automatic release, Discord notifications, and escape detection
- Multi-server support — manage multiple Conan Exiles servers from one bot instance
- Teleporter task — processes queued teleports from jail releases and admin commands
- External watchdog (watchdog.py) — auto-restarts the bot on crash with scheduled periodic restarts
- systemd service file (conan-shop.service) included for Linux deployments
- RCON health monitoring — detects connection failures and fast-fails tasks during outages, auto-recovers when RCON responds
- Server settings watcher — monitors game.db for setting changes and posts Discord alerts
- Firewall blocklist management — block/unblock IPs via netsh (Windows) or iptables (Linux)
- Admin commands for currency, items, teleport, broadcast, and moderation

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11 or newer | [python.org/downloads](https://www.python.org/downloads/) |
| MariaDB | 10.6 or newer | Or use the included Docker Compose setup |
| Git | Any recent version | [git-scm.com](https://git-scm.com/download/win) |
| Conan Exiles dedicated server | — | RCON must be enabled |
| Discord bot token | — | [discord.com/developers](https://discord.com/developers/applications) |

**Enabling RCON on your Conan Exiles server:**

Open `ConanSandbox/Saved/Config/WindowsServer/Engine.ini` and add:

```ini
[OnlineSubsystemSteam]
RconEnabled=True
RconPassword=your_password_here
RconPort=25575
```

Restart the server after saving.

---

## Installation

### Step 1 — Download the bot

```bash
git clone https://github.com/aquesada97/Conan-Exiles-Shop.git
cd Conan-Exiles-Shop
```

If you do not have Git installed, download and install it from [git-scm.com](https://git-scm.com/download/win), then run the commands above in a new Command Prompt window.

### Step 2 — Create a virtual environment

A virtual environment keeps the bot's dependencies isolated from other Python projects.

**Windows:**
```cmd
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

You should see `(.venv)` at the start of your terminal prompt after activation.

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

This installs all required packages including discord.py, database drivers, and the scheduler.

### Step 4 — Create your configuration file

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Open `.env` in any text editor and fill in every value. See the [Configuration Reference](#configuration-reference) section for a full explanation of each setting.

---

## Configuration Reference

All settings are stored in the `.env` file. Below is a description of each section.

### Database (MariaDB)

| Key | Default | Description |
|---|---|---|
| DB_HOST | 127.0.0.1 | MariaDB server hostname or IP |
| DB_PORT | 3306 | MariaDB port |
| DB_USER | (required) | Database username |
| DB_PASS | (required) | Database password |
| DB_NAME | (required) | Database name (must exist before running setup) |

### Conan Exiles Server

| Key | Default | Description |
|---|---|---|
| SERVER_NAME | ConanExiles | Prefix for per-server tables. Alphanumeric only, no spaces. |
| RCON_HOST | 127.0.0.1 | RCON host |
| RCON_PORT | 25575 | RCON port |
| RCON_PASS | (required) | RCON password |
| STEAM_QUERY_PORT | 27015 | Steam query port for player count queries |
| GAME_DB_PATH | (required) | Full path to the `game.db` SQLite file |
| GAME_LOG_PATH | (required) | Full path to `ConanSandbox.log` |

On Windows, use forward slashes or double backslashes in paths:
```
GAME_DB_PATH=C:/Conan/ConanSandbox/Saved/game.db
GAME_LOG_PATH=C:/Conan/ConanSandbox/Saved/Logs/ConanSandbox.log
```

### Discord

| Key | Default | Description |
|---|---|---|
| DISCORD_TOKEN | (required) | Your bot token from the Discord developer portal |
| KILLLOG_CHANNEL_ID | 0 | Channel where kills are posted in real time |
| SOLO_LB_1D_CHANNEL_ID | 0 | Solo kill leaderboard — last 24 hours |
| SOLO_LB_7D_CHANNEL_ID | 0 | Solo kill leaderboard — last 7 days |
| SOLO_LB_30D_CHANNEL_ID | 0 | Solo kill leaderboard — last 30 days |
| SOLO_LB_ALL_CHANNEL_ID | 0 | Solo kill leaderboard — all time |
| CLAN_LB_1D_CHANNEL_ID | 0 | Clan kill leaderboard — last 24 hours |
| CLAN_LB_7D_CHANNEL_ID | 0 | Clan kill leaderboard — last 7 days |
| CLAN_LB_30D_CHANNEL_ID | 0 | Clan kill leaderboard — last 30 days |
| CLAN_LB_ALL_CHANNEL_ID | 0 | Clan kill leaderboard — all time |
| BUILDING_TRACKING_CHANNEL_ID | 0 | Building piece leaderboard channel |
| INVENTORY_TRACKING_CHANNEL_ID | 0 | Inventory leaderboard channel |
| WANTED_CHANNEL_ID | 0 | Wanted player list channel |
| JAIL_CHANNEL_ID | 0 | Jail event notifications channel |
| SERVER_BUFFS_CHANNEL_ID | 0 | Server buff notifications channel |
| VAULT_RENTAL_CHANNEL_ID | 0 | Vault rental notifications channel |
| EVENT_CHANNEL_ID | 0 | General event announcements channel |

Set any channel to `0` to disable that feature's Discord notifications.

### Shop and Economy

| Key | Default | Description |
|---|---|---|
| STARTING_CASH | 100 | Coins given to new players on first login |
| PAYCHECK | 50 | Coins earned per payroll cycle |
| PAYCHECK_INTERVAL_MINUTES | 30 | How often payroll runs |
| CURRENCY_NAME | coins | Display name for the currency |

### Black Ice Converter

| Key | Default | Description |
|---|---|---|
| BLACK_ICE_ITEM_ID | 18040 | Conan Exiles template ID for Black Ice |
| HARDENED_BRICK_ITEM_ID | 11142 | Conan Exiles template ID for Hardened Brick |
| BLACK_ICE_CONVERSION_RATE | 10 | How many Black Ice equal one Hardened Brick |
| BLACK_ICE_CHECK_INTERVAL_SECONDS | 120 | How often the converter runs (seconds) |

### Jail / Prison

| Key | Default | Description |
|---|---|---|
| PRISON_ENABLED | false | Enable escape detection and auto-release |
| PRISON_EXIT_COORDS | 0 0 0 | X Y Z coordinates players teleport to on release |
| PRISON_MIN_X | 0 | Prison zone boundary (minimum X) |
| PRISON_MAX_X | 0 | Prison zone boundary (maximum X) |
| PRISON_MIN_Y | 0 | Prison zone boundary (minimum Y) |
| PRISON_MAX_Y | 0 | Prison zone boundary (maximum Y) |

### Discord Roles

| Key | Default | Description |
|---|---|---|
| ADMIN_ROLE | Admin | Discord role name with full admin access |
| MOD_ROLE | Moderator | Discord role name with moderator access |
| VIP1_ROLE | VIP1 | First VIP tier role name |
| VIP2_ROLE | VIP2 | Second VIP tier role name |
| VIP3_ROLE | VIP3 | Third VIP tier role name |
| VIP4_ROLE | VIP4 | Fourth VIP tier role name |
| TIMEZONE_OFFSET | -6 | UTC offset in hours to match your server log timestamps |
| MAP_URL | (empty) | Optional URL to your interactive server map. If set, a link is added to all leaderboard Discord embeds. |
| SERVER_SETTINGS_CHANNEL_ID | 0 | Channel for server settings change alerts |
| FIREWALL_ENABLED | false | Enable OS-level IP blocking via /addblock and /removeblock |
| FIREWALL_BLOCKLIST_FILE | blocklist.txt | Path to the IP blocklist file |

---

## Database Setup

Make sure MariaDB is running and the database named in `DB_NAME` already exists. Then run:

```bash
python setup_db.py
```

This creates all required tables and inserts a server record from your `.env` values. You only need to run this once. If you add new features later and need to update the schema, run it again — all `CREATE TABLE` statements use `IF NOT EXISTS` and will not overwrite existing data.

### Adding items to the shop

Connect to your MariaDB instance and insert rows into `shop_items`. The `itemType` controls how the item is delivered (see [Shop Item Types](#shop-item-types)).

```sql
-- Single item (spawned directly into player inventory)
INSERT INTO shop_items (itemName, itemDescription, itemPrice, itemid, itemType, category, isActive)
VALUES ('Iron Sword', 'A basic iron sword.', 50, '51021', 'single', 'Weapons', 1);

-- Knowledge item (teaches a feat via LearnFeat)
INSERT INTO shop_items (itemName, itemDescription, itemPrice, itemid, itemType, category, isActive)
VALUES ('Stonecutter Knowledge', 'Unlocks the stonecutter table.', 200, '18252', 'knowledge', 'Knowledge', 1);

-- Vault rental (priced per day)
INSERT INTO shop_items (itemName, itemDescription, itemPrice, itemid, itemType, category, isActive)
VALUES ('Small Vault', 'A small personal storage vault.', 75, 'vault_small_1', 'vault', 'Rentals', 1);
```

---

## Running the Bot

Activate your virtual environment first, then:

```bash
python -m bot.main
```

The bot will:
1. Connect to MariaDB and initialise the connection pool
2. Start all background tasks (payroll, user sync, order processing, etc.)
3. Connect to Discord and sync slash commands
4. Begin tailing the Conan server log

Slash commands can take up to one hour to propagate globally on Discord. For instant testing, see the [Troubleshooting](#troubleshooting) section.

---

## Docker Deployment

Docker is the recommended way to run the bot in production. It handles the MariaDB dependency automatically.

### Prerequisites

- Docker Desktop (Windows/macOS) or Docker Engine + Docker Compose (Linux)
- Download: [docs.docker.com/get-docker](https://docs.docker.com/get-docker/)

### Step 1 — Prepare your .env file

Complete your `.env` file as described in the Configuration Reference. Make sure `GAME_DB_PATH` and `GAME_LOG_PATH` point to the actual files on your host machine — they are mounted read-only into the container.

Set `DB_HOST=mariadb` (the Docker Compose service name) instead of `127.0.0.1`.

### Step 2 — Start all services

```bash
docker compose up -d
```

This starts MariaDB and the bot. The bot waits for MariaDB to pass its health check before connecting.

### Step 3 — Initialize the database (first run only)

```bash
docker compose exec bot python setup_db.py
```

### Step 4 — Check the logs

```bash
docker compose logs -f bot
```

### Common Docker commands

```bash
# Restart only the bot (after updating .env or code)
docker compose restart bot

# Stop all services
docker compose down

# Stop all services and delete the database volume
docker compose down -v

# View recent bot logs
docker compose logs --tail=100 bot
```

---

## Discord Commands

### Player Commands

| Command | Description |
|---|---|
| `/register` | Generates a one-time code. Type `!register <code>` in-game to link your account. |
| `/balance` | Shows your current coin balance. |
| `/shop` | Lists all available shop items. Optionally filter by category: `/shop Weapons`. |
| `/buy <item_name>` | Purchases an item. Delivery is automatic — instant if you are online, queued if offline. |
| `/listvaults` | Shows vaults available to rent and their daily price. |
| `/rentvault <vault_name> <days>` | Rents a vault for 1 to 30 days. Cost is deducted immediately. |
| `/myvaults` | Lists your active vault rentals and their expiration times. |
| `/releasevault <vault_name>` | Releases a vault rental early. No refund is issued. |

### Admin Commands

These commands require the Admin or Moderator Discord role configured in `.env`.

| Command | Description |
|---|---|
| `/givecurrency <user> <amount>` | Adds coins to a player's wallet by Discord mention. |
| `/giveitem <platform_id> <template_id> <qty>` | Spawns an item on an online player via RCON. |
| `/jail <character> <minutes> [reason]` | Teleports a player to the prison coordinates for the given duration. |
| `/teleport <character> <x> <y> <z>` | Teleports an online player to the specified coordinates. |
| `/broadcast <message>` | Sends a message to all online players via RCON. |
| `/processblackice` | Manually triggers the Black Ice to Hardened Brick conversion cycle. |
| `/wanted [player_name]` | Marks a player as wanted (level 3), or shows the current wanted list if no name is given. |
| `/bounty <player_name> <amount>` | Sets a coin bounty on a player. |
| `/addblock <ip_address>` | Adds an IP address or CIDR range to the firewall blocklist. Requires `FIREWALL_ENABLED=true`. |
| `/removeblock <ip_address>` | Removes an IP address or CIDR range from the firewall blocklist. |

---

## Background Tasks

The following tasks run automatically on a fixed schedule:

| Task | Interval | Description |
|---|---|---|
| User sync | Every 5 minutes | Syncs the online player list from RCON into the database. Creates accounts for new players. |
| Order processing | Every 5 seconds | Delivers one pending shop order. Retries failed orders after 5 minutes. |
| Payroll | Configurable (default 30 min) | Pays all currently online players their paycheck amount. |
| Black Ice converter | Every 2 minutes | Converts pending Black Ice drops into Hardened Bricks at the configured rate. |
| Game DB watcher | Every 1 minute | Syncs building piece counts and inventory counts from game.db. Releases prisoners whose sentence has expired, sends Discord notification, and detects escapes. |
| Teleporter | Every 2 seconds | Executes queued TeleportPlayer RCON commands (jail releases, admin teleports). |
| Server buff watcher | Every 1 minute | Deactivates server buffs whose duration has elapsed. |
| Vault watcher | Every 5 minutes | Marks expired vault rentals as inactive. Posts expiration notices to Discord. |
| Building leaderboard | Every 1 minute | Posts building piece and inventory count leaderboards to Discord. |
| Kill leaderboards | Every 10 minutes | Posts solo and clan kill leaderboards across all four time windows. |
| Wanted watcher | Every 30 minutes | Degrades wanted levels for inactive players. Cleans up old PVP position data. |
| Server settings watcher | Every 5 minutes | Reads game.db for server setting changes and posts Discord alerts when values differ from the last snapshot. |
| Firewall sync | Every 1 minute | Syncs the blocklist file with active firewall rules. Only runs when `FIREWALL_ENABLED=true`. |
| Log watcher | Continuous | Tails the Conan server log for kills, Black Ice drops, and in-game chat commands. |

All per-server tasks run independently for each server in the `servers` table. Adding a new server row automatically starts its own task set on next bot restart.

---

## Shop Item Types

The `itemType` column in `shop_items` controls how the order is fulfilled:

| Type | Delivery Method |
|---|---|
| `single` | `spawnitem <template_id> <qty>` via RCON directly into the player's inventory |
| `kit` | Same as `single` — used to distinguish kits visually in the shop |
| `knowledge` | `LearnFeat <template_id>` via RCON — teaches the player a feat or knowledge |
| `serverBuff` | Runs the `activateCommand` from the `server_buffs` table — no player target required |
| `vault` | Creates a rental record in `{server_name}_vault_rentals` — admin must assign access in-game |

---

## Black Ice Converter

Players drop Black Ice in-game. The bot detects these drops from the server log and accumulates them. Every two minutes, the converter runs and awards Hardened Bricks at the configured ratio.

How it works:

1. Player drops Black Ice in-game
2. The log watcher detects the drop and records it in the database
3. Every 2 minutes, the converter groups pending drops by player
4. Bricks awarded = floor(total Black Ice / conversion rate)
5. Any remainder carries over to the next cycle
6. If the player is online, items are delivered instantly via RCON
7. If the player is offline, the order is queued for delivery on next login

Example: A player drops 35 Black Ice across several drops. The converter awards 3 Hardened Bricks and carries over 5 Black Ice to the next cycle.

Default item IDs (configurable in `.env`):
- Black Ice: `18040`
- Hardened Brick: `11142`
- Conversion rate: `10:1`

---

## Kill Tracking and Leaderboards

Kill events are detected from the server log in real time and stored in the database with player names, platform IDs, and coordinates.

Solo leaderboards rank individual players by kill count. Clan leaderboards group players by clan tag prefix (e.g. `[CLAN] PlayerName`).

Four time windows are tracked independently for each board type:

| Window | Discord Setting |
|---|---|
| Last 24 hours | `SOLO_LB_1D_CHANNEL_ID` / `CLAN_LB_1D_CHANNEL_ID` |
| Last 7 days | `SOLO_LB_7D_CHANNEL_ID` / `CLAN_LB_7D_CHANNEL_ID` |
| Last 30 days | `SOLO_LB_30D_CHANNEL_ID` / `CLAN_LB_30D_CHANNEL_ID` |
| All time | `SOLO_LB_ALL_CHANNEL_ID` / `CLAN_LB_ALL_CHANNEL_ID` |

Each leaderboard edits a single pinned message in the configured channel rather than posting new messages every cycle.

---

## Wanted Player System

Players who kill others accumulate a kill streak which increases their wanted level. Wanted levels automatically degrade if the player has not killed anyone in 48 hours.

| Wanted Level | Label | Approximate Streak |
|---|---|---|
| 0 | Clean | No recent kills |
| 1 | Minor | 1-2 kills |
| 2 | Notable | 3-5 kills |
| 3 | Dangerous | 6-10 kills |
| 4 | Feared | 11-20 kills |
| 5 | Most Wanted | 21+ kills |

Admins can manually set a player as wanted with `/wanted <player_name>` or place a coin bounty with `/bounty <player_name> <amount>`. The wanted list is posted to the channel configured in `WANTED_CHANNEL_ID` and updated every 30 minutes.

---

## Vault Rental System

Vaults are defined as items in `shop_items` with `itemType = 'vault'`. The `itemPrice` is the cost per day and `itemid` is the vault's unique identifier.

```sql
INSERT INTO shop_items (itemName, itemDescription, itemPrice, itemid, itemType, isActive)
VALUES ('Vault Row A-1', 'Small vault in Row A.', 50, 'vault_a1', 'vault', 1);
```

Players rent vaults with `/rentvault`. The bot tracks the rental period and posts an expiration notice when it ends. Physical vault access (pin codes, ownership) must still be managed in-game by an admin.

---

## Server Buffs

Server buffs are timed effects that run RCON commands on the entire server (not targeted at a specific player). They are added directly to the `server_buffs` table and purchased through the shop.

```sql
INSERT INTO server_buffs (serverName, buffName, buffDescription, buffPrice, duration_minutes, activateCommand, deactivateCommand)
VALUES (
  'ConanExiles',
  'XP Boost 2x',
  'Doubles experience gain for all players for 60 minutes.',
  300,
  60,
  'SetServerSetting XPRateMultiplier 2',
  'SetServerSetting XPRateMultiplier 1'
);
```

Then link it to the shop by adding a `serverBuff` item where `itemid` matches the `server_buffs.id`:

```sql
INSERT INTO shop_items (itemName, itemDescription, itemPrice, itemid, itemType, isActive)
VALUES ('XP Boost 2x', 'Doubles XP for all players for 60 minutes.', 300, '1', 'serverBuff', 1);
```

The buff watcher automatically runs the `deactivateCommand` when the duration expires and posts a notification to the `SERVER_BUFFS_CHANNEL_ID` channel.

---

## Jail System

Admins send players to jail with `/jail <character> <minutes> [reason]`. The bot teleports the player to the coordinates in `PRISON_EXIT_COORDS` and records the sentence.

When `PRISON_ENABLED=true`, the game DB watcher checks every minute whether sentenced players are still within the prison zone boundaries.

- When a sentence expires, the player is added to the teleport queue, released from jail, and a Discord embed is posted to the jail channel.
- If a jailed player moves outside the prison boundary before their sentence expires, the bot detects the escape, teleports them back, and posts a Discord notification.

Prison zone boundaries are set with:
```
PRISON_MIN_X, PRISON_MAX_X, PRISON_MIN_Y, PRISON_MAX_Y
```

---

## Multi-Server Support

The bot can manage multiple Conan Exiles servers from a single instance. Add one row per server to the `servers` table:

```sql
INSERT INTO servers (ServerName, DatabaseLocation, LogLocation, Prison_Exit_Coordinates, Enabled)
VALUES (
    'MyServer',
    '/path/to/game.db',
    '/path/to/ConanSandbox.log',
    '100000 200000 -3600',
    1
);
```

On startup, the bot reads all rows where `Enabled = 1` and starts a full set of background tasks for each server. If the `servers` table is empty or unreachable, the bot falls back to the single-server configuration in `.env`.

Each server uses its own table prefix (`{ServerName}_currentusers`, `{ServerName}_teleport_requests`, etc.), its own log watcher, and its own RCON connection.

---

## Watchdog (Process Supervisor)

`watchdog.py` wraps the bot process and automatically restarts it on crash. Use this instead of running `bot.main` directly in production.

```bash
python watchdog.py
```

Configuration via environment variables (or `.env`):

| Variable | Default | Description |
|---|---|---|
| `WATCHDOG_RESTART_HOURS` | `6` | Scheduled full restart interval in hours. Set to `0` to disable. |
| `WATCHDOG_RESTART_DELAY` | `5` | Seconds to wait between a crash and a restart attempt. |
| `WATCHDOG_MAX_CRASHES` | `10` | Maximum crashes within the crash window before watchdog gives up. |
| `WATCHDOG_CRASH_WINDOW` | `60` | Time window in seconds for counting rapid crashes. |

Watchdog logs to `logs/watchdog.log`.

On Linux with systemd, use the included `conan-shop.service` unit file instead:

```bash
# Edit the paths in the file first, then:
sudo cp conan-shop.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now conan-shop
sudo journalctl -u conan-shop -f
```

---

## RCON Health Monitoring

The bot tracks RCON connectivity per server. If a server's RCON fails 5 consecutive times, it is marked unhealthy. While unhealthy:

- All RCON-dependent tasks (payroll, usersync, teleporter, etc.) fail immediately instead of retrying for up to 50 seconds, keeping the task scheduler responsive.
- The bot continues attempting the connection on each task cycle with a reduced timeout.
- As soon as any RCON command succeeds, the server is marked healthy again and normal retry behavior resumes.
- Status changes are logged at ERROR level (unhealthy) and INFO level (recovered).

No configuration is required. This behavior is always active.

---

## Server Settings Watcher

The server settings watcher reads the `properties` table from `game.db` every 5 minutes and compares it to a stored snapshot in the `{SN}_server_settings_snapshot` MariaDB table.

On first run it builds the baseline snapshot without posting any alerts. On subsequent runs, any key whose value differs from the snapshot is included in a Discord embed posted to the `SERVER_SETTINGS_CHANNEL_ID` channel.

To enable Discord notifications, set in `.env`:
```
SERVER_SETTINGS_CHANNEL_ID=<your_discord_channel_id>
```

If the `properties` table does not exist in `game.db`, the watcher looks for any 2-column table whose name contains "setting", "config", or "propert".

---

## Firewall Blocklist

The firewall module allows admins to block IP addresses at the OS level directly from Discord.

Enable in `.env`:
```
FIREWALL_ENABLED=true
FIREWALL_BLOCKLIST_FILE=blocklist.txt
```

The blocklist file contains one IP address or CIDR range per line. Lines starting with `#` are treated as comments.

The firewall sync task runs every minute and ensures the active firewall rules match the file contents. Rules are applied using `netsh advfirewall` on Windows and `iptables` on Linux.

Admin commands:

- `/addblock <ip_address>` — Blocks the IP immediately and adds it to the blocklist file.
- `/removeblock <ip_address>` — Unblocks the IP immediately and removes it from the file.

Note: On Linux, `iptables` rules are not persistent across reboots by default. Install `iptables-persistent` to persist them, or rely on the bot applying rules on startup via the scheduled sync task.

---



The log watcher uses regular expressions to detect events. If your server version or mods produce a different log format, open `bot/tasks/game_log_watcher.py` and adjust the relevant pattern.

Kill events:
```python
RE_KILL = re.compile(
    r"'(?P<victim>[^']+)'\s+was\s+killed\s+by\s+'?(?P<killer>[^'\[]+?)'?\s*(?:\[|$)",
    re.IGNORECASE,
)
```

Black Ice drops:
```python
RE_BLACK_ICE_DROP = re.compile(
    r"(?P<char>.+?)\s+dropped\s+Black\s*Ice\s+(?:amount:|x)(?P<amount>\d+)",
    re.IGNORECASE,
)
```

To find the correct format, enable `DEBUG` logging and observe how the lines appear in `logs/bot.log` when the event occurs in-game.

---

## Troubleshooting

**Slash commands do not appear in Discord**

Global slash commands can take up to one hour to propagate. To sync instantly to a specific server during testing, edit `bot/main.py` and change:

```python
synced = await bot.tree.sync()
```

to:

```python
synced = await bot.tree.sync(guild=discord.Object(id=YOUR_SERVER_ID))
```

Replace `YOUR_SERVER_ID` with your Discord server's ID. To find it: right-click the server icon and select Copy Server ID (you may need to enable Developer Mode in Discord settings under Advanced).

**RCON connection refused**

- Confirm `RCON_HOST`, `RCON_PORT`, and `RCON_PASS` match what is in `Engine.ini`
- Ensure `RconEnabled=True` is set in `Engine.ini`
- Check that the RCON port is not blocked by a firewall
- Test manually: `rcon -H 127.0.0.1 -P 25575 -p yourpassword listplayers`

**Black Ice drops are not being detected**

- Confirm `GAME_LOG_PATH` points to the live log file, not a copy or archive
- Drop Black Ice in-game and search `logs/bot.log` for a line containing `dropped`
- If no line appears, the log format differs from the regex — update `RE_BLACK_ICE_DROP`

**Players are not being synced**

- Check that `listplayers` returns output when run manually via RCON
- Review `logs/bot.log` for errors from the user sync task

**game.db errors**

- The bot opens `game.db` as read-only. Ensure the path in `GAME_DB_PATH` is correct and the file exists
- On a live server, `game.db` is usually located at: `ConanSandbox/Saved/game.db`

**Items are not delivered after purchase**

- Check that the player is online and their `platformid` is correctly linked in the `accounts` table
- Review `order_processing` in the database for rows with `completed=0` and check the `last_attempt` timestamp
- Orders are retried every 5 minutes — the player must be online at the time of retry

**Enable verbose logging**

Edit `bot/main.py` and change the console log level from `INFO` to `DEBUG`:

```python
logger.add(sys.stderr, level="DEBUG", ...)
```

---

## Tech Stack

| Library | Purpose |
|---|---|
| discord.py 2.x | Discord bot, slash commands, Cogs |
| APScheduler 3.x | Async task scheduling |
| aiomysql | Async MariaDB connection pool |
| aiosqlite | Read-only async access to game.db |
| rcon | RCON protocol client |
| pydantic-settings | Config validation from .env |
| loguru | Structured logging with file rotation |
| aiofiles | Async server log tailing |
| Pillow (optional) | Map image generation |

---

## Credits

Based on work from [irrelevantgamers/Conan-Shop](https://github.com/irrelevantgamers/Conan-Shop) and [irrelevantgamers/IG_Bot](https://github.com/irrelevantgamers/IG_Bot), licensed under GPL-3.0.

This project is released under the **GPL-3.0 License**.

