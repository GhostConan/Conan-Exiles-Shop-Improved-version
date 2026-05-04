"""
bot/config.py
─────────────
All configuration is loaded from the .env file via pydantic-settings.
Every value is validated at startup — missing required fields raise a clear error.
"""
from __future__ import annotations

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
    vip1_role: str = "VIP1"
    vip2_role: str = "VIP2"

    # ── Timezone ──────────────────────────────────────────────────────────────
    timezone_offset: int = -6


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
