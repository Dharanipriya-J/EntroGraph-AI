"""Transparent, configurable heuristic risk engine.

Layers kept apart on purpose:
  * observed facts   - rows returned by FalkorDB (paths, sessions, orders)
  * computed metrics - entropy, normalised entropy, JS divergence (app/metrics.py)
  * heuristic scores - THIS module: weighted points defined in RiskConfig
  * AI interpretation - app/interpreter.py (text only; it never changes a score)

The score is a risk ASSESSMENT used to prioritise human review. It is not proof of fraud.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import RiskConfig
from .metrics import BehaviorMetrics


@dataclass
class Counterpart:
    customer_id: str
    name: str
    customer_hops: int
    rel_count: int
    link_class: str  # private_device | ip_weak | public_shared
    path_nodes: list[dict]
    rel_types: list[str]
    path_display: str
    established: bool
    tenure_days: float
    drift: float | None
    created_at: int
    n_events: int
    coupon_recent: bool
    alt_paths: int = 0


@dataclass
class GraphFindings:
    traversal_enabled: bool
    max_customer_hops: int
    counterparts: list[Counterpart] = field(default_factory=list)
    ignored_links: list[dict] = field(default_factory=list)  # public hubs / kiosks / public-only paths
    coupon_cluster_size: int = 0
    coupon_cluster_key: tuple[str, str] | None = None
    coupon_cluster_members: list[str] = field(default_factory=list)
    coupon_cluster_span_days: float | None = None
    age_proximity_ids: list[str] = field(default_factory=list)
    memory_high_ids: list[str] = field(default_factory=list)


@dataclass
class TargetFacts:
    customer_id: str
    tenure_days: float
    prior_orders: int


@dataclass
class Component:
    name: str
    group: str  # behavioural | graph | mitigation
    max_points: float
    points: float
    rationale: str
    inputs: dict = field(default_factory=dict)


@dataclass
class RiskAssessment:
    score: float
    band: str
    flagged: bool
    components: list[Component]
    config_version: str
    behavioural_points: float
    graph_points: float
    mitigation_points: float
    normalized_behavioural_score: float  # behavioural points scaled to 0-100 (used by the isolated baseline)


def _frac(x: float, sat: float) -> float:
    return 0.0 if sat <= 0 else max(0.0, min(1.0, x / sat))


def band_for(score: float, cfg: RiskConfig) -> str:
    return "high" if score >= cfg.band_high else "medium" if score >= cfg.band_medium else "low"


def behavioural_components(m: BehaviorMetrics, cfg: RiskConfig) -> list[Component]:
    comps: list[Component] = []
    if m.drift_composite is None:
        comps.append(Component("behavioural_drift", "behavioural", cfg.w_drift, 0.0,
                               f"Not scored: {'; '.join(m.notes) or 'insufficient data'}.", {"status": m.status}))
    else:
        pts = cfg.w_drift * _frac(m.drift_composite, cfg.drift_saturation)
        comps.append(Component("behavioural_drift", "behavioural", cfg.w_drift, round(pts, 2),
                               f"Jensen-Shannon divergence between baseline and recent behaviour = {m.drift_composite:.3f} bits "
                               f"(saturates at {cfg.drift_saturation}). Drift alone is common in legitimate customers.",
                               {"drift_composite": m.drift_composite, "drift_category": m.drift_category, "drift_hour": m.drift_hour}))
    if m.entropy_shift is None:
        comps.append(Component("entropy_shift", "behavioural", cfg.w_entropy_shift, 0.0, "Not scored: drift not computable.", {}))
    else:
        pts = cfg.w_entropy_shift * _frac(abs(m.entropy_shift), cfg.entropy_shift_saturation)
        comps.append(Component("entropy_shift", "behavioural", cfg.w_entropy_shift, round(pts, 2),
                               f"Change in normalised category entropy = {m.entropy_shift:+.3f}. Only the CHANGE is scored; a high or low "
                               "absolute entropy is not evidence of fraud.", {"entropy_shift": m.entropy_shift}))
    comps.append(Component("new_device_recent", "behavioural", cfg.w_new_device, cfg.w_new_device if m.new_devices_recent else 0.0,
                           f"Devices used only in the recent window: {m.new_devices_recent or 'none'}. A new device is often a new phone.",
                           {"new_devices": m.new_devices_recent}))
    comps.append(Component("recent_coupon_use", "behavioural", cfg.w_recent_coupon, cfg.w_recent_coupon if m.recent_coupon_orders else 0.0,
                           f"{m.recent_coupon_orders} order(s) with a coupon in the recent window. Coupons are normal promotions.",
                           {"orders": m.recent_coupon_orders}))
    return comps


def graph_components(f: GraphFindings, target: TargetFacts, cfg: RiskConfig) -> list[Component]:
    strong = [c for c in f.counterparts if c.link_class == "private_device" and not c.established]
    weak = [c for c in f.counterparts if c.link_class == "ip_weak" and not c.established]
    comps = [
        Component("linked_accounts_private_device", "graph", cfg.w_linked_accounts,
                  round(cfg.w_linked_accounts * _frac(len(strong), cfg.linked_accounts_saturation), 2),
                  f"{len(strong)} not-yet-established account(s) reachable through a private device within {f.max_customer_hops} customer hop(s): "
                  f"{[c.customer_id for c in strong] or 'none'}. Established, stable accounts (household use) are not counted.",
                  {"accounts": [c.customer_id for c in strong]}),
        Component("shared_ip_weak", "graph", cfg.w_shared_ip,
                  round(cfg.w_shared_ip * _frac(len(weak), 2), 2),
                  f"{len(weak)} account(s) linked only through a residential/mobile IP: {[c.customer_id for c in weak] or 'none'}. "
                  "Shared IPs are weak evidence (carrier NAT, households).", {"accounts": [c.customer_id for c in weak]}),
    ]
    size = f.coupon_cluster_size
    pts = 0.0
    if size >= cfg.coupon_cluster_min_customers:
        pts = cfg.w_coupon_cluster * _frac(size - 1, max(1, cfg.coupon_cluster_saturation - 1))
    comps.append(Component("coupon_cluster", "graph", cfg.w_coupon_cluster, round(pts, 2),
                           (f"{size} linked account(s) incl. the target redeemed coupon/category {f.coupon_cluster_key} within "
                            f"{cfg.coupon_window_days} days: {f.coupon_cluster_members}. A widely advertised coupon alone is not suspicious; "
                            "it only counts together with a graph link.") if size else "No coupon/category overlap among linked accounts.",
                           {"size": size, "key": list(f.coupon_cluster_key) if f.coupon_cluster_key else None}))
    comps.append(Component("account_age_proximity", "graph", cfg.w_age_proximity, cfg.w_age_proximity if f.age_proximity_ids else 0.0,
                           f"Linked accounts created within {cfg.age_proximity_hours}h of the target: {f.age_proximity_ids or 'none'}.",
                           {"accounts": f.age_proximity_ids}))
    mem_on = cfg.use_memory
    comps.append(Component("memory_escalation", "graph", cfg.w_memory_escalation,
                           cfg.w_memory_escalation if (mem_on and f.memory_high_ids) else 0.0,
                           f"Earlier stored investigations rated linked accounts high: {f.memory_high_ids or 'none'}"
                           + ("" if mem_on else " (memory disabled in this configuration)") + ".",
                           {"accounts": f.memory_high_ids}))
    return comps


def mitigation_components(f: GraphFindings, target: TargetFacts, cfg: RiskConfig) -> list[Component]:
    on = cfg.use_mitigations
    established = on and target.tenure_days >= cfg.established_tenure_days and target.prior_orders >= cfg.established_min_orders
    comps = [Component("established_tenure", "mitigation", -cfg.m_established_tenure,
                       -cfg.m_established_tenure if established else 0.0,
                       f"Account age {target.tenure_days:.0f} days with {target.prior_orders} earlier orders "
                       f"(needs >= {cfg.established_tenure_days} days and >= {cfg.established_min_orders} orders). "
                       "A long clean history is a benign explanation for unusual behaviour.",
                       {"tenure_days": round(target.tenure_days, 1), "prior_orders": target.prior_orders})]
    linked = [c for c in f.counterparts if c.link_class in ("private_device", "ip_weak")]
    household = on and bool(linked) and all(c.established for c in linked)
    comps.append(Component("household_context", "mitigation", -cfg.m_household_context,
                           -cfg.m_household_context if household else 0.0,
                           "All accounts linked to the target are long-standing and behaviourally stable, which is consistent with household "
                           f"sharing (linked: {[c.customer_id for c in linked] or 'none'})."
                           , {"linked": [c.customer_id for c in linked]}))
    return comps


def assess(metrics: BehaviorMetrics, findings: GraphFindings | None, target: TargetFacts, cfg: RiskConfig) -> RiskAssessment:
    beh = behavioural_components(metrics, cfg)
    graph = graph_components(findings, target, cfg) if findings else []
    mit = mitigation_components(findings or GraphFindings(False, 0), target, cfg) if findings else \
        mitigation_components(GraphFindings(False, 0), target, cfg)
    if findings is None:  # isolated mode never uses profile context
        mit = []
    b, g, m = (sum(c.points for c in x) for x in (beh, graph, mit))
    score = max(0.0, min(100.0, b + g + m))
    norm_b = 0.0 if cfg.behaviour_max <= 0 else 100.0 * b / cfg.behaviour_max
    return RiskAssessment(score=round(score, 2), band=band_for(score, cfg), flagged=score >= cfg.flag_threshold,
                          components=beh + graph + mit, config_version=cfg.version(), behavioural_points=round(b, 2),
                          graph_points=round(g, 2), mitigation_points=round(m, 2),
                          normalized_behavioural_score=round(norm_b, 2))
