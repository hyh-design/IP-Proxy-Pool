"""Application settings with secure, validated defaults."""

from functools import lru_cache

from pydantic import AnyHttpUrl, BaseModel, Field, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ip_proxy_pool.models import TestTarget
from ip_proxy_pool.security.network import CLOUDFLARE_IPV4_NETWORKS


class RedisSettings(BaseModel):
    url: RedisDsn = RedisDsn("redis://localhost:6379/0")
    key_prefix: str = Field("ippool:dev", min_length=1, max_length=128)
    connect_timeout_seconds: float = Field(2.0, ge=0.1, le=30)
    read_timeout_seconds: float = Field(5.0, ge=0.1, le=60)


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    auth_enabled: bool = True
    api_keys: tuple[SecretStr, ...] = ()
    admin_api_keys: tuple[SecretStr, ...] = ()
    cursor_secret: SecretStr | None = None
    max_page_size: int = Field(200, ge=1, le=1000)
    query_rate_limit: int = Field(120, ge=1, le=10_000)
    admin_rate_limit: int = Field(10, ge=1, le=1000)
    rate_window_seconds: int = Field(60, ge=1, le=3600)
    admin_probe_enabled: bool = False
    legacy_routes_enabled: bool = False


class CollectorSettings(BaseModel):
    concurrency: int = Field(20, ge=1, le=200)
    max_pages_per_source: int = Field(5, ge=1, le=100)
    max_response_bytes: int = Field(2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    max_proxies_per_source_round: int = Field(2_000, ge=1, le=100_000)
    max_pool_size_per_domain: int = Field(10_000, ge=1, le=1_000_000)
    collection_interval_seconds: int = Field(300, ge=30, le=86_400)
    inventory_check_interval_seconds: int = Field(60, ge=10, le=3600)
    low_inventory_threshold: int = Field(20, ge=0, le=20)
    low_inventory_min_score: int = Field(80, ge=0, le=100)
    low_inventory_max_latency_ms: float = Field(2000, ge=100, le=60_000)
    low_inventory_max_new_candidates: int = Field(500, ge=1, le=10_000)


class CheckerSettings(BaseModel):
    concurrency: int = Field(100, ge=1, le=500)
    batch_size: int = Field(200, ge=1, le=1000)
    lease_seconds: int = Field(60, ge=5, le=600)
    request_timeout_seconds: float = Field(5.0, ge=0.5, le=30)
    breaker_failures: int = Field(3, ge=1, le=20)
    breaker_cooldown_seconds: int = Field(60, ge=5, le=3600)
    drop_after_failures: int = Field(5, ge=3, le=20)


class SelectionSettings(BaseModel):
    min_score: int = Field(90, ge=0, le=100)
    max_latency_ms: float = Field(5000, ge=100, le=60_000)
    max_checked_age_seconds: int = Field(600, ge=30, le=86_400)
    min_consecutive_successes: int = Field(2, ge=1, le=20)


class SecuritySettings(BaseModel):
    allow_non_global_proxies: bool = False
    allowed_probe_hosts: tuple[str, ...] = ()
    allow_redirects: bool = False
    blocked_proxy_networks: tuple[str, ...] = CLOUDFLARE_IPV4_NETWORKS


class ObservabilitySettings(BaseModel):
    log_level: str = "INFO"
    json_logs: bool = True
    metrics_enabled: bool = True


class ValidationTargetSettings(BaseModel):
    name: str = Field("ipify", min_length=1, max_length=64)
    url: AnyHttpUrl = AnyHttpUrl("https://api.ipify.org?format=json")
    expected_statuses: frozenset[int] = frozenset({200})
    expected_text: str | None = None
    json_keys: tuple[str, ...] = ("ip",)
    timeout_seconds: float = Field(5.0, ge=0.5, le=30)

    def to_test_target(self, *, domain: str) -> TestTarget:
        return TestTarget.model_validate({**self.model_dump(), "domain": domain})


class TargetSettings(BaseModel):
    name: str = Field("httpbin", min_length=1, max_length=64)
    url: AnyHttpUrl = AnyHttpUrl("https://httpbin.org/ip")
    domain: str = Field("httpbin.org", min_length=1, max_length=253)
    expected_statuses: frozenset[int] = frozenset({200})
    expected_text: str | None = None
    json_keys: tuple[str, ...] = ("origin",)
    timeout_seconds: float = Field(5.0, ge=0.5, le=30)
    validation_targets: tuple[ValidationTargetSettings, ...] = Field(
        default_factory=lambda: (ValidationTargetSettings(),)
    )

    def to_test_target(self) -> TestTarget:
        payload = self.model_dump(exclude={"validation_targets"})
        return TestTarget.model_validate(payload)

    def to_validation_targets(self) -> tuple[TestTarget, ...]:
        return tuple(
            target.to_test_target(domain=self.domain) for target in self.validation_targets
        )


class DashboardSettings(BaseModel):
    enabled: bool = True
    snapshot_interval_seconds: int = Field(300, ge=60, le=3600)
    short_retention_hours: int = Field(48, ge=24, le=168)
    long_retention_hours: int = Field(720, ge=168, le=2160)
    heartbeat_interval_seconds: int = Field(30, ge=10, le=300)
    heartbeat_ttl_seconds: int = Field(90, ge=30, le=900)
    refresh_seconds: int = Field(30, ge=10, le=300)
    max_aggregate_records: int = Field(20_000, ge=100, le=100_000)


class Settings(BaseSettings):
    """Root settings loaded from environment and an optional local `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="IP_POOL_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    environment: str = Field("dev", min_length=1, max_length=32)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    collector: CollectorSettings = Field(default_factory=CollectorSettings)
    checker: CheckerSettings = Field(default_factory=CheckerSettings)
    selection: SelectionSettings = Field(default_factory=SelectionSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    target: TargetSettings = Field(default_factory=TargetSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)

    def validate_api_startup(self) -> None:
        """Validate secrets required only by the API runtime role."""
        errors: list[str] = []
        normal_keys = [item.get_secret_value() for item in self.api.api_keys]
        admin_keys = [item.get_secret_value() for item in self.api.admin_api_keys]
        all_keys = normal_keys + admin_keys
        if self.api.auth_enabled and not all_keys:
            errors.append("at least one API key is required when authentication is enabled")
        if any(not item for item in all_keys):
            errors.append("API keys must be non-empty")
        if len(all_keys) != len(set(all_keys)):
            errors.append("API keys must be unique across roles")

        cursor_secret = self.api.cursor_secret
        if cursor_secret is None or len(cursor_secret.get_secret_value()) < 32:
            errors.append("cursor secret must contain at least 32 characters")

        if self.api.admin_probe_enabled:
            if not self.api.admin_api_keys:
                errors.append("an admin API key is required for the admin probe")
            if not self.security.allowed_probe_hosts:
                errors.append("an allowed probe host is required for the admin probe")
        if errors:
            raise ValueError("; ".join(errors))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings snapshot."""
    return Settings()
