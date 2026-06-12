"""
bot/config.py
─────────────
All configuration is loaded from the .env file via pydantic-settings.
Every value is validated at startup — missing required fields raise a clear error.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── MariaDB ──────────────────────────────────────────────────────────────
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str
    db_pass: str
    db_name: str

    # ── Conan Exiles server ───────────────────────────────────────────────────
    server_name: str = "ConanExiles"
    rcon_host: str = "127.0.0.1"
    rcon_port: int = 25575
    rcon_pass: str
    steam_query_port: int = 27015
    game_db_path: str
    game_log_path: str

    # ── Discord ───────────────────────────────────────────────────────────────
    discord_token: str
    serverlog_channel_id: int = 0
    killlog_channel_id: int = 0
    items_for_sale_channel_id: int = 0
    server_buffs_channel_id: int = 0
    jail_channel_id: int = 0
    event_channel_id: int = 0
    solo_lb_all_channel_id: int = 0
    solo_lb_1d_channel_id: int = 0
    solo_lb_7d_channel_id: int = 0
    solo_lb_30d_channel_id: int = 0
    clan_lb_all_channel_id: int = 0
    clan_lb_1d_channel_id: int = 0
    clan_lb_7d_channel_id: int = 0
    clan_lb_30d_channel_id: int = 0
    building_tracking_channel_id: int = 0
    inventory_tracking_channel_id: int = 0
    wanted_channel_id: int = 0
    vault_rental_channel_id: int = 0
    server_settings_channel_id: int = 0  # channel for server setting change alerts

    # ── Map URL (optional) ────────────────────────────────────────────────────
    # If set, a link to your server map is added to leaderboard embeds.
    map_url: str = ""

    # ── Shop ──────────────────────────────────────────────────────────────────
    starting_cash: int = 100
    paycheck: int = 50
    paycheck_interval_minutes: int = 30
    currency_name: str = "coins"

    # ── Black Ice Converter ───────────────────────────────────────────────────
    black_ice_item_id: int = 18040
    hardened_brick_item_id: int = 11142
    black_ice_conversion_rate: int = 10          # 10 black ice → 1 hardened brick
    black_ice_check_interval_seconds: int = 120  # run every 2 minutes

    # ── Prison ────────────────────────────────────────────────────────────────
    prison_enabled: bool = False
    prison_exit_coords: str = "0 0 0"
    prison_min_x: int = 0
    prison_max_x: int = 0
    prison_min_y: int = 0
    prison_max_y: int = 0

    # ── Discord role names ─────────────────────────────────────────────────────
    admin_role: str = "Admin"
    mod_role: str = "Moderator"
    adminbot_role: str = "AdminBot"
    vip1_role: str = "VIP1"
    vip2_role: str = "VIP2"
    vip3_role: str = "VIP3"
    vip4_role: str = "VIP4"

    # ── Timezone ──────────────────────────────────────────────────────────────
    timezone_offset: int = -6

    # ── Firewall blocklist ────────────────────────────────────────────────────
    firewall_enabled: bool = False
    firewall_blocklist_file: str = "blocklist.txt"

    # ── Raid tracker ──────────────────────────────────────────────────────────
    # Set RAID_ALERT_CHANNEL_ID to a Discord channel id to enable raid alerts.
    # While a raid window is active (started via /raidstart), the bot polls
    # building piece counts every RAID_CHECK_INTERVAL_SECONDS and posts an
    # embed whenever a clan's loss since the previous post exceeds
    # RAID_ALERT_THRESHOLD pieces. A per-clan cooldown of
    # RAID_ALERT_COOLDOWN_SECONDS suppresses spam during long fights.
    raid_alert_channel_id: int = 0
    raid_alert_threshold: int = 10
    raid_alert_cooldown_seconds: int = 60
    raid_check_interval_seconds: int = 10

    # ── Raid window (scheduled) ───────────────────────────────────────────────
    # When RAID_WINDOW_ENABLED is true, raid_watcher automatically opens a
    # raid window every day between RAID_WINDOW_START and RAID_WINDOW_END
    # (24-hour HH:MM in the RAID_WINDOW_TZ timezone). Manual /raidstart still
    # works outside the scheduled window. Windows that cross midnight are
    # supported (e.g. start=22:00 end=02:00).
    raid_window_enabled: bool = False
    raid_window_start: str = "18:00"
    raid_window_end: str = "22:00"
    raid_window_tz: str = "America/New_York"

    # ── Rebuild-under-attack detection ────────────────────────────────────────
    # During an active raid window, the watcher reads damage events directly
    # from game.db.game_events (eventType in RAID_DAMAGE_EVENT_TYPES) so a
    # "break + instant rebuild" within the same server save tick is still
    # caught even when the net piece count is unchanged. If a clan places new
    # pieces (or restores destroyed ones) while they have taken raid damage
    # within the last RAID_REBUILD_DAMAGE_LOOKBACK_SECONDS, an embed is posted
    # to the SERVERLOG channel. Default lookback is 15 minutes — clans are
    # expected to wait that long after the last damage before repairing.
    raid_rebuild_damage_lookback_seconds: int = 900
    raid_rebuild_min_pieces: int = 1
    # CSV of game_events.eventType values that count as building damage.
    # Default 172 matches current Conan builds where each building piece
    # destruction emits an event with eventType=172 and ownerGuildId set
    # to the owning clan's guildId. Legacy builds used 91-94 (no owner
    # attribution); set the CSV to whatever your game.db uses.
    raid_damage_event_types: str = "172"

    # ── Shrine tracker (optional) ─────────────────────────────────────────────
    # When SHRINE_CHANNEL_ID is set, the shrine_watcher polls game.db for all
    # placeables whose class is listed in SHRINE_CLASSES (CSV of full BP
    # paths). Every SHRINE_CHECK_INTERVAL_SECONDS it edits a single pinned
    # leaderboard embed in that channel showing the per-clan shrine count
    # and posts a "💥 Shrine Destroyed" alert whenever a tracked shrine
    # disappears from actor_position. The destroyer name (if any) is
    # pulled from destruction_history.
    shrine_channel_id: int = 0
    shrine_classes: str = (
        "/Game/Systems/Building/Placeables/BP_PL_Altar_Yog_T3.BP_PL_Altar_Yog_T3_C"
    )
    shrine_check_interval_seconds: int = 60

    # ── Embed timestamps ──────────────────────────────────────────────────────
    # When TIMESTAMP_FOOTER=true, every event embed adds a "Server time:
    # YYYY-MM-DD HH:MM:SS TZ" suffix to its footer so the host's wall-clock
    # time is visible to every viewer regardless of their Discord locale.
    # The native embed timestamp (rendered in each viewer's local TZ) is
    # always set; this is purely additive.
    timestamp_footer: bool = False

    # ── Kill catch-up ─────────────────────────────────────────────────────────
    # On bot startup, replay any PvP kills that happened in game.db.game_events
    # while the bot was offline. Set KILL_CATCHUP_ENABLED=false to skip replay
    # entirely (the cursor is still advanced to "now" so re-enabling later won't
    # dump the whole backlog). KILL_CATCHUP_MAX_REPLAY caps each burst.
    kill_catchup_enabled: bool = True
    kill_catchup_max_replay: int = 500

    # ── Building piece tracking ───────────────────────────────────────────────
    # SQL LIKE pattern applied to building_instances.class when materialising
    # the per-clan piece count into {sn}_building_piece_tracking. Filters out
    # non-structural placeables (bombs, orbs, traps, banners, torches,
    # bedrolls, etc.) so the count — and the raid rebuild detector — only
    # respond to real building pieces. Default catches every BP_Build* class
    # on current Conan builds. Override if a future patch renames things.
    building_piece_class_like: str = "%BP_Build%"

    # How often the game.db -> MariaDB materialiser runs. This is what feeds
    # per-clan piece counts (rebuild detection) and the building leaderboard.
    # Default 60s is fine for most servers; lower values (e.g. 5) make rebuild
    # detection snappier but require ServerSaveInterval in Conan to be set
    # equally low or the underlying game.db won't have refreshed yet.
    game_db_watcher_interval_seconds: int = 60


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# ── Per-server configuration ────────────────────────────────────────────────────
@dataclass
class ServerContext:
    """Per-server configuration loaded from the ``servers`` table or from .env.

    Global settings (Discord token, DB credentials, shop values, channel IDs)
    remain in ``Settings``.  ``ServerContext`` holds only the per-instance
    Conan Exiles server configuration.
    """

    server_name: str
    rcon_host: str
    rcon_port: int
    rcon_pass: str
    game_db_path: str
    game_log_path: str
    prison_exit_coords: str = "0 0 0"
    prison_min_x: int = 0
    prison_min_y: int = 0
    prison_max_x: int = 0
    prison_max_y: int = 0

    @property
    def prison_enabled(self) -> bool:
        """True when jail bounds have been configured."""
        return bool(self.prison_max_x or self.prison_max_y)

    @classmethod
    def from_db_row(cls, row: dict) -> "ServerContext":
        """Build from a DictCursor row from the ``servers`` table.
        Any missing/null column falls back to the corresponding .env value.
        """
        s = settings
        return cls(
            server_name=row["ServerName"],
            rcon_host=row.get("rcon_host") or s.rcon_host,
            rcon_port=int(row.get("rcon_port") or s.rcon_port),
            rcon_pass=row.get("rcon_pass") or s.rcon_pass,
            game_db_path=row.get("DatabaseLocation") or s.game_db_path,
            game_log_path=row.get("LogLocation") or s.game_log_path,
            prison_exit_coords=row.get("Prison_Exit_Coordinates") or s.prison_exit_coords,
            prison_min_x=int(row.get("prison_min_x") or s.prison_min_x),
            prison_min_y=int(row.get("prison_min_y") or s.prison_min_y),
            prison_max_x=int(row.get("prison_max_x") or s.prison_max_x),
            prison_max_y=int(row.get("prison_max_y") or s.prison_max_y),
        )

    @classmethod
    def from_settings(cls) -> "ServerContext":
        """Build from global .env settings — single-server fallback."""
        s = settings
        return cls(
            server_name=s.server_name,
            rcon_host=s.rcon_host,
            rcon_port=s.rcon_port,
            rcon_pass=s.rcon_pass,
            game_db_path=s.game_db_path,
            game_log_path=s.game_log_path,
            prison_exit_coords=s.prison_exit_coords,
            prison_min_x=s.prison_min_x,
            prison_min_y=s.prison_min_y,
            prison_max_x=s.prison_max_x,
            prison_max_y=s.prison_max_y,
        )
