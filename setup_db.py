"""
setup_db.py
────────────
Run ONCE to create all required MariaDB tables.

    python setup_db.py

The script reads your .env file automatically.
SERVER_NAME in .env must match the server name you configured — it is used
as a prefix for all per-server tables (e.g. ConanExiles_currentusers).
"""
from __future__ import annotations

import asyncio
import aiomysql
from bot.config import settings

SN = settings.server_name  # short alias used for per-server table names


TABLES: list[str] = [
    # ── Global tables ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS accounts (
        ID                INT          AUTO_INCREMENT PRIMARY KEY,
        conanplayer       VARCHAR(100),
        conanuserid       VARCHAR(100),
        conanplatformid   VARCHAR(100) UNIQUE,
        steamplatformid   VARCHAR(100),
        walletbalance     INT          NOT NULL DEFAULT 0,
        lastPaid          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        lastupdated       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        firstseen         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        earnratemultiplier INT         NOT NULL DEFAULT 1,
        discordid         VARCHAR(50),
        lastServer        VARCHAR(100)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    """
    CREATE TABLE IF NOT EXISTS registration_codes (
        ID               INT          AUTO_INCREMENT PRIMARY KEY,
        discordID        VARCHAR(50)  NOT NULL,
        registrationcode VARCHAR(20)  NOT NULL UNIQUE,
        curstatus        TINYINT(1)   NOT NULL DEFAULT 0,
        created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    """
    CREATE TABLE IF NOT EXISTS servers (
        ID                      INT          AUTO_INCREMENT PRIMARY KEY,
        ServerName              VARCHAR(100) UNIQUE NOT NULL,
        dedicated               TINYINT(1)   NOT NULL DEFAULT 1,
        rcon_host               VARCHAR(100),
        rcon_port               INT,
        rcon_pass               VARCHAR(100),
        SteamQueryPort          INT,
        DatabaseLocation        TEXT,
        LogLocation             TEXT,
        Enabled                 TINYINT(1)   NOT NULL DEFAULT 1,
        lastUserSync            DATETIME,
        Prison_Exit_Coordinates VARCHAR(100),
        prison_min_x            INT DEFAULT 0,
        prison_min_y            INT DEFAULT 0,
        prison_max_x            INT DEFAULT 0,
        prison_max_y            INT DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    """
    CREATE TABLE IF NOT EXISTS shop_items (
        ID              INT          AUTO_INCREMENT PRIMARY KEY,
        itemName        VARCHAR(200) NOT NULL,
        itemDescription TEXT,
        itemPrice       INT          NOT NULL DEFAULT 0,
        itemid          VARCHAR(50),
        itemType        VARCHAR(50)  NOT NULL DEFAULT 'single',
        category        VARCHAR(100) NOT NULL DEFAULT 'General',
        serverName      VARCHAR(100),
        isActive        TINYINT(1)   NOT NULL DEFAULT 1
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    """
    CREATE TABLE IF NOT EXISTS order_processing (
        ID                   INT          AUTO_INCREMENT PRIMARY KEY,
        order_number         VARCHAR(50)  NOT NULL,
        itemid               VARCHAR(50),
        itemType             VARCHAR(50),
        itemcount            INT          NOT NULL DEFAULT 1,
        purchaser_platformid VARCHAR(100),
        purchaser_steamid    VARCHAR(100),
        order_date           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed            TINYINT(1)   NOT NULL DEFAULT 0,
        in_process           TINYINT(1)   NOT NULL DEFAULT 0,
        refunded             TINYINT(1)   NOT NULL DEFAULT 0,
        completed_date       DATETIME,
        last_attempt         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        serverName           VARCHAR(100),
        INDEX idx_order_status (completed, in_process, refunded, last_attempt)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    """
    CREATE TABLE IF NOT EXISTS shop_log (
        ID           INT    AUTO_INCREMENT PRIMARY KEY,
        order_number VARCHAR(50),
        discordID    VARCHAR(50),
        itemid       VARCHAR(50),
        itemName     VARCHAR(200),
        quantity     INT,
        totalCost    INT,
        orderDate    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        curstatus    VARCHAR(50) NOT NULL DEFAULT 'Pending'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    """
    CREATE TABLE IF NOT EXISTS server_buffs (
        id                INT          AUTO_INCREMENT PRIMARY KEY,
        serverName        VARCHAR(100),
        buffName          VARCHAR(200),
        buffDescription   TEXT,
        buffPrice         INT          NOT NULL DEFAULT 0,
        duration_minutes  INT          NOT NULL DEFAULT 60,
        isactive          TINYINT(1)   NOT NULL DEFAULT 0,
        activateCommand   TEXT,
        deactivateCommand TEXT,
        lastActivated     DATETIME,
        lastActivatedBy   VARCHAR(50),
        endTime           DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    # ── Per-server tables (prefixed with SERVER_NAME) ──────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {SN}_currentusers (
        ID             INT          AUTO_INCREMENT PRIMARY KEY,
        conid          VARCHAR(20),
        player         VARCHAR(200),
        userid         VARCHAR(200),
        platformid     VARCHAR(100),
        steamPlatformId VARCHAR(100),
        X              INT          NOT NULL DEFAULT 0,
        Y              INT          NOT NULL DEFAULT 0,
        loadDate       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_platform (platformid)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_building_piece_tracking (
        ID                  INT  AUTO_INCREMENT PRIMARY KEY,
        clan_id             INT,
        clan_name           VARCHAR(200),
        building_piece_count INT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_inventory_tracking (
        ID              INT  AUTO_INCREMENT PRIMARY KEY,
        clan_id         INT,
        clan_name       VARCHAR(200),
        inventory_count INT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_pendingDiscordMsg (
        ID            INT   AUTO_INCREMENT PRIMARY KEY,
        message       TEXT,
        messageType   VARCHAR(50) NOT NULL DEFAULT 'channel',
        destChannelID VARCHAR(50),
        sent          TINYINT(1) NOT NULL DEFAULT 0,
        created_at    DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_jail_info (
        ID                        INT  AUTO_INCREMENT PRIMARY KEY,
        cellName                  VARCHAR(100),
        prisoner                  VARCHAR(200),
        sentenceTime              DATETIME,
        sentenceLength            INT,
        assignedPlayerPlatformID  VARCHAR(100),
        spawnLocation             VARCHAR(200)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_teleport_requests (
        ID          INT  AUTO_INCREMENT PRIMARY KEY,
        player      VARCHAR(200),
        dstlocation VARCHAR(200),
        platformid  VARCHAR(100),
        processed   TINYINT(1) NOT NULL DEFAULT 0,
        created_at  DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_homelocations (
        ID           INT  AUTO_INCREMENT PRIMARY KEY,
        player       VARCHAR(200),
        platformid   VARCHAR(100) UNIQUE,
        homelocation VARCHAR(200)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_vault_rentals (
        ID               INT  AUTO_INCREMENT PRIMARY KEY,
        vaultName        VARCHAR(200),
        renterdiscordid  VARCHAR(50),
        renterplatformid VARCHAR(100),
        rentedUntil      DATETIME,
        inUse            TINYINT(1) NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_kill_log (
        ID               INT          AUTO_INCREMENT PRIMARY KEY,
        killer_name      VARCHAR(200),
        killer_platformid VARCHAR(100),
        victim_name      VARCHAR(200),
        victim_platformid VARCHAR(100),
        kill_x           INT          NOT NULL DEFAULT 0,
        kill_y           INT          NOT NULL DEFAULT 0,
        kill_time        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_killer  (killer_platformid, kill_time),
        INDEX idx_victim  (victim_platformid, kill_time),
        INDEX idx_time    (kill_time)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_wanted_players (
        ID           INT          AUTO_INCREMENT PRIMARY KEY,
        player       VARCHAR(200),
        platformid   VARCHAR(100) UNIQUE,
        kill_streak  INT          NOT NULL DEFAULT 0,
        wanted_level INT          NOT NULL DEFAULT 0,
        bounty       INT          NOT NULL DEFAULT 0,
        last_kill    DATETIME,
        last_seen    DATETIME,
        INDEX idx_wanted (wanted_level, last_kill)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_recent_pvp (
        ID        INT          AUTO_INCREMENT PRIMARY KEY,
        pvpname   VARCHAR(400),
        x         INT          NOT NULL DEFAULT 0,
        y         INT          NOT NULL DEFAULT 0,
        loadDate  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_loaddate (loadDate)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {SN}_historicalusers (
        ID              INT          AUTO_INCREMENT PRIMARY KEY,
        conid           VARCHAR(20),
        player          VARCHAR(200),
        userid          VARCHAR(200),
        platformid      VARCHAR(100),
        steamPlatformId VARCHAR(100),
        X               INT          NOT NULL DEFAULT 0,
        Y               INT          NOT NULL DEFAULT 0,
        loadDate        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_platform (platformid),
        INDEX idx_loaddate (loadDate)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    # ── Black Ice Converter table ──────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {SN}_black_ice_pending (
        ID          INT      AUTO_INCREMENT PRIMARY KEY,
        platform_id VARCHAR(100) NOT NULL,
        amount      INT          NOT NULL DEFAULT 0,
        drop_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        processed   TINYINT(1)   NOT NULL DEFAULT 0,
        INDEX idx_pending (platform_id, processed)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,

    # ── Server settings snapshot table ────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {SN}_server_settings_snapshot (
        setting_key   VARCHAR(255) PRIMARY KEY,
        setting_value TEXT,
        updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
]

# Seed the servers table with values from .env
SEED_SERVER = f"""
    INSERT IGNORE INTO servers
        (ServerName, rcon_host, rcon_port, rcon_pass, SteamQueryPort,
         DatabaseLocation, LogLocation, Enabled,
         Prison_Exit_Coordinates, prison_min_x, prison_min_y, prison_max_x, prison_max_y)
    VALUES
        (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s);
"""


async def run() -> None:
    conn = await aiomysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_pass,
        db=settings.db_name,
        charset="utf8mb4",
        autocommit=True,
    )
    async with conn.cursor() as cur:
        await cur.execute("SET NAMES utf8mb4")
        for sql in TABLES:
            await cur.execute(sql.strip())
            print(f"  ✓  {sql.strip().splitlines()[0][:60]}…")

        await cur.execute(
            SEED_SERVER,
            (
                SN,
                settings.rcon_host, settings.rcon_port, settings.rcon_pass,
                settings.steam_query_port,
                settings.game_db_path, settings.game_log_path,
                settings.prison_exit_coords,
                settings.prison_min_x, settings.prison_min_y,
                settings.prison_max_x, settings.prison_max_y,
            ),
        )
        print(f"  ✓  server row for '{SN}' upserted into servers table")

    conn.close()
    print("\n✅ Database setup complete!")


if __name__ == "__main__":
    asyncio.run(run())
