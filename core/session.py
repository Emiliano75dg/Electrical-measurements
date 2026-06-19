"""Session persistence — save/load the complete measurement setup as JSON.

REDESIGN.md Phase 3.  A Session captures everything needed to reconstruct a
measurement setup — connection, the declarative source/meter channels, the
derived-quantity choice, sample geometry and acquisition timing — so the user
can close the app and reopen an identical setup from a file.

Kept deliberately Qt-free and at the domain level (SI units, the typed config
dataclasses of core.channels).  The GUI assembles a Session from its panels and
applies a loaded one back; this module only knows the data model and JSON I/O.
The same Session is the unit validate_configuration() (core.validation) checks
before an acquisition starts.

The schema is versioned (SCHEMA_VERSION).  Matrix layout and route steps are
part of the model (added in Phase 4 without a version bump — older files simply
lack those keys and load with defaults).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.channels import Func, MeterConfig, SourceConfig
from core.derived import Geometry
from measurements.routing import MatrixLayout, RouteStep

SCHEMA_VERSION = 1


@dataclass
class ConnectionSettings:
    ip_address: str = "192.168.0.1"
    simulated: bool = True


@dataclass
class MatrixSettings:
    enabled: bool = False
    resource: str = ""
    simulated: bool = True
    settle_s: float = 0.05
    vdp_sheet: bool = False        # compute van der Pauw R_sheet (cross-step) live


@dataclass
class SourceSpec:
    port: int
    config: SourceConfig


@dataclass
class MeterSpec:
    port: int
    meter_id: str
    config: MeterConfig


@dataclass
class Session:
    """The complete, serialisable measurement setup."""

    connection: ConnectionSettings = field(default_factory=ConnectionSettings)
    sources: list[SourceSpec] = field(default_factory=list)
    meters: list[MeterSpec] = field(default_factory=list)
    derived_mode: str = "Hall preset (Rxx, Rxy, ρ)"
    geometry: Geometry = field(default_factory=Geometry)
    settle_s: float = 1.0
    interval_s: float = 0.5
    current_reversal: bool = False
    matrix: MatrixSettings = field(default_factory=MatrixSettings)
    layout: MatrixLayout = field(default_factory=MatrixLayout)
    routes: list[RouteStep] = field(default_factory=list)

    # ── serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "connection": asdict(self.connection),
            "sources": [
                {"port": s.port, "config": _source_cfg_to_dict(s.config)}
                for s in self.sources
            ],
            "meters": [
                {"port": m.port, "meter_id": m.meter_id, "config": asdict(m.config)}
                for m in self.meters
            ],
            "derived_mode": self.derived_mode,
            "geometry": asdict(self.geometry),
            "settle_s": self.settle_s,
            "interval_s": self.interval_s,
            "current_reversal": self.current_reversal,
            "matrix": asdict(self.matrix),
            "layout": {
                "terminal_row": dict(self.layout.terminal_row),
                "contact_col": dict(self.layout.contact_col),
            },
            "routes": [
                {"label": r.label, "links": [list(link) for link in r.links]}
                for r in self.routes
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        conn = d.get("connection", {})
        connection = ConnectionSettings(
            ip_address=conn.get("ip_address", ConnectionSettings.ip_address),
            simulated=conn.get("simulated", ConnectionSettings.simulated),
        )
        sources = [
            SourceSpec(port=int(s["port"]), config=_source_cfg_from_dict(s.get("config", {})))
            for s in d.get("sources", [])
        ]
        meters = [
            MeterSpec(
                port=int(m["port"]),
                meter_id=m.get("meter_id", f"M{m['port']}"),
                config=_meter_cfg_from_dict(m.get("config", {})),
            )
            for m in d.get("meters", [])
        ]
        geo = d.get("geometry", {})
        geometry = Geometry(
            width_m=geo.get("width_m", 0.0),
            length_m=geo.get("length_m", 0.0),
            thickness_m=geo.get("thickness_m", 0.0),
        )
        mx = d.get("matrix", {})
        matrix = MatrixSettings(
            enabled=mx.get("enabled", MatrixSettings.enabled),
            resource=mx.get("resource", MatrixSettings.resource),
            simulated=mx.get("simulated", MatrixSettings.simulated),
            settle_s=mx.get("settle_s", MatrixSettings.settle_s),
            vdp_sheet=mx.get("vdp_sheet", MatrixSettings.vdp_sheet),
        )
        lay = d.get("layout", {})
        layout = MatrixLayout(
            terminal_row={k: int(v) for k, v in lay.get("terminal_row", {}).items()},
            contact_col={k: int(v) for k, v in lay.get("contact_col", {}).items()},
        )
        routes = [
            RouteStep(
                label=r.get("label", f"step{i}"),
                links=[(link[0], link[1]) for link in r.get("links", [])],
            )
            for i, r in enumerate(d.get("routes", []))
        ]
        defaults = cls()
        return cls(
            connection=connection,
            sources=sources,
            meters=meters,
            derived_mode=d.get("derived_mode", defaults.derived_mode),
            geometry=geometry,
            settle_s=d.get("settle_s", defaults.settle_s),
            interval_s=d.get("interval_s", defaults.interval_s),
            current_reversal=d.get("current_reversal", defaults.current_reversal),
            matrix=matrix,
            layout=layout,
            routes=routes,
        )


# ── config (de)serialisation helpers ─────────────────────────────────────────

def _source_cfg_to_dict(cfg: SourceConfig) -> dict:
    d = asdict(cfg)
    d["func"] = cfg.func.value          # store the enum by its stable string value
    return d


def _source_cfg_from_dict(d: dict) -> SourceConfig:
    base = SourceConfig()
    return SourceConfig(
        func=Func(d.get("func", base.func.value)),
        amplitude=d.get("amplitude", base.amplitude),
        frequency_Hz=d.get("frequency_Hz", base.frequency_Hz),
        compliance=d.get("compliance", base.compliance),
    )


def _meter_cfg_from_dict(d: dict) -> MeterConfig:
    base = MeterConfig()
    return MeterConfig(
        lockin=d.get("lockin", base.lockin),
        reference=d.get("reference", base.reference),
        harmonic=d.get("harmonic", base.harmonic),
        time_constant_s=d.get("time_constant_s", base.time_constant_s),
        rolloff=d.get("rolloff", base.rolloff),
        phase_shift_deg=d.get("phase_shift_deg", base.phase_shift_deg),
        use_fir=d.get("use_fir", base.use_fir),
        nplc=d.get("nplc", base.nplc),
        smu=d.get("smu", base.smu),
    )


# ── file I/O ──────────────────────────────────────────────────────────────────

def save_session(session: Session, path: str | Path) -> None:
    """Write a Session to a JSON file."""
    with open(Path(path), "w", encoding="utf-8") as fh:
        json.dump(session.to_dict(), fh, indent=2, ensure_ascii=False)


def load_session(path: str | Path) -> Session:
    """Read a Session from a JSON file. Raises on an unsupported newer schema."""
    with open(Path(path), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    version = data.get("schema_version", 0)
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"Setup saved with schema v{version}, supported up to v{SCHEMA_VERSION}. "
            "Update the application."
        )
    return Session.from_dict(data)
