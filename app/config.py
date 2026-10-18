"""Runtime settings and the transparent, configurable risk configuration."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    """Connection and runtime settings (read from environment variables)."""

    db_mode: str = field(default_factory=lambda: _env("FALKORDB_MODE", "server"))  # server | embedded
    db_host: str = field(default_factory=lambda: _env("FALKORDB_HOST", "localhost"))
    db_port: int = field(default_factory=lambda: int(_env("FALKORDB_PORT", "6379")))
    db_password: str | None = field(default_factory=lambda: os.environ.get("FALKORDB_PASSWORD") or None)
    embedded_path: str = field(default_factory=lambda: _env("FALKORDB_EMBEDDED_PATH", "./data/entrograph.rdb"))
    graph_name: str = field(default_factory=lambda: _env("GRAPH_NAME", "entrograph"))
    llm_mode: str = field(default_factory=lambda: _env("ENTROGRAPH_LLM", "deterministic"))  # deterministic | anthropic
    llm_model: str = field(default_factory=lambda: _env("ENTROGRAPH_LLM_MODEL", "claude-sonnet-5-5"))
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY") or None)
    auto_load_scenario: bool = field(default_factory=lambda: _env("AUTO_LOAD_SCENARIO", "true").lower() == "true")


@dataclass
class RiskConfig:
    """Every number used by the heuristic risk engine lives here (nothing is hidden in code).

    Points are additive. Behavioural components can be computed from the target's own
    events only (the isolated baseline). Graph components need FalkorDB traversal.
    Mitigations subtract points. The total is clipped to [0, 100].
    """

    # --- behavioural windows -------------------------------------------------
    recent_days: int = 14
    min_baseline_events: int = 8
    min_recent_events: int = 3
    category_universe_size: int = 10  # used to normalise entropy of product categories
    hour_universe_size: int = 4  # four 6-hour buckets
    drift_weight_category: float = 0.7
    drift_weight_hour: float = 0.3
    # --- behavioural weights (max points) ------------------------------------
    w_drift: float = 20.0
    drift_saturation: float = 0.5  # JS divergence (bits) at which the drift component is maxed
    w_entropy_shift: float = 10.0
    entropy_shift_saturation: float = 0.4  # |change in normalised entropy| that maxes the component
    w_new_device: float = 5.0
    w_recent_coupon: float = 5.0
    # --- graph weights (max points) ------------------------------------------
    w_linked_accounts: float = 20.0  # non-established accounts linked through a private device
    linked_accounts_saturation: int = 2
    w_shared_ip: float = 5.0  # weak: non-public IP only
    w_coupon_cluster: float = 20.0
    coupon_cluster_min_customers: int = 2  # customers (incl. target) needed to count at all
    coupon_cluster_saturation: int = 3  # customers (incl. target) that max the component
    coupon_window_days: int = 10
    w_age_proximity: float = 10.0
    age_proximity_hours: int = 72
    w_memory_escalation: float = 5.0
    # --- mitigations (negative points) ---------------------------------------
    m_established_tenure: float = 15.0
    established_tenure_days: int = 90
    established_min_orders: int = 3
    m_household_context: float = 10.0
    established_max_drift: float = 0.2
    # --- graph traversal -----------------------------------------------------
    max_customer_hops: int = 2  # 0 disables traversal; 1 = direct; 2 = friend-of-friend
    path_limit: int = 300
    # --- interpretation ------------------------------------------------------
    band_medium: float = 30.0
    band_high: float = 60.0
    flag_threshold: float = 50.0  # used by the evaluation to turn a score into a yes/no flag
    use_memory: bool = True
    use_mitigations: bool = True

    @property
    def behaviour_max(self) -> float:
        return self.w_drift + self.w_entropy_shift + self.w_new_device + self.w_recent_coupon

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RiskConfig":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def version(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:10]


def get_settings() -> Settings:
    return Settings()


def get_risk_config() -> RiskConfig:
    """Defaults, optionally overridden by a JSON file named in RISK_CONFIG_PATH (unknown keys are ignored)."""
    path = os.environ.get("RISK_CONFIG_PATH")
    if path and os.path.exists(path):
        with open(path) as fh:
            return RiskConfig.from_dict(json.load(fh))
    return RiskConfig()
