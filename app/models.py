"""Typed inputs and outputs shared by the agent tools and the API."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .scenarios import CATEGORIES

ID_PATTERN = r"^[A-Za-z0-9_\-:.]{1,64}$"
EventType = Literal["view", "add_to_cart", "purchase"]
DeviceKind = Literal["phone", "laptop", "tablet", "smart_tv", "kiosk"]
NetworkType = Literal["residential", "mobile_carrier", "public_wifi", "corporate_nat"]


class CustomerInput(BaseModel):
    customer_id: str = Field(pattern=ID_PATTERN)


class TraverseInput(BaseModel):
    customer_id: str = Field(pattern=ID_PATTERN)
    max_customer_hops: int = Field(default=2, ge=0, le=3, description="0 disables traversal; each hop = customer->session->infra->session->customer")


class MemoryInput(BaseModel):
    customer_id: str = Field(pattern=ID_PATTERN)
    entity_ids: list[str] = Field(default_factory=list, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


class EventIn(BaseModel):
    """A synthetic behavioural event to ingest. Validated before anything touches the graph."""

    customer_id: str = Field(pattern=ID_PATTERN)
    type: EventType = "view"
    category: str
    device_id: str = Field(pattern=ID_PATTERN)
    ip_id: str = Field(pattern=ID_PATTERN)
    session_id: str | None = Field(default=None, pattern=ID_PATTERN)
    device_kind: DeviceKind = "phone"
    network_type: NetworkType = "residential"
    amount: float | None = Field(default=None, ge=0, le=100000)
    coupon: str | None = Field(default=None, pattern=ID_PATTERN)
    order_id: str | None = Field(default=None, pattern=ID_PATTERN)
    ts: int | None = Field(default=None, ge=1577836800, le=4102444800, description="unix seconds; defaults to the scenario clock")

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        return v


class InvestigateRequest(BaseModel):
    customer_id: str = Field(pattern=ID_PATTERN)
    persist: bool = True
    max_customer_hops: int | None = Field(default=None, ge=0, le=3)


class ResetRequest(BaseModel):
    scenario: Literal["default-v1"] = "default-v1"


# ---------------------------------------------------------------- tool outputs
@dataclass
class CustomerContext:
    customer: dict
    sessions: list[dict]
    events: list[dict]
    orders: list[dict]
    ref_ts: int
    scenario: str | None


@dataclass
class TraversalResult:
    enabled: bool
    max_customer_hops: int
    paths: list[dict] = field(default_factory=list)  # best path per reachable customer
    all_path_count: int = 0
    shared_devices: list[dict] = field(default_factory=list)
    shared_ips: list[dict] = field(default_factory=list)
    hubs: list[dict] = field(default_factory=list)


@dataclass
class MemoryResult:
    same_target: list[dict] = field(default_factory=list)
    related: list[dict] = field(default_factory=list)


@dataclass
class EvidenceItem:
    key: str
    kind: str  # supporting | contradicting | missing
    layer: str  # observed_fact | computed_metric | heuristic_score
    text: str
    refs: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


@dataclass
class EvidenceBundle:
    supporting: list[EvidenceItem]
    contradicting: list[EvidenceItem]
    missing: list[EvidenceItem]
    benign_explanations: list[str]
    findings: object  # risk.GraphFindings
