"""Loads a JSON config and derives every account, amount and id the referenda use."""

import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from scalecodec.utils.ss58 import ss58_encode

from .encoding import (
    DOT_DECIMALS,
    JAMKB_DECIMALS,
    keyless_account_id,
    planks,
    pool_account_id,
    task_id,
    treasury_account_id,
)

SS58_POLKADOT = 0

# Some PalletIds in use on Asset Hub. Not a complete list: `account_seed` must differ from every
# pallet's PalletId, including the one `pallet-jamkb` will use.
KNOWN_PALLET_IDS = {"py/trsry", "py/ascon", "py/astrd"}


def addr(account_id: bytes) -> str:
    return ss58_encode(account_id, SS58_POLKADOT)


@dataclass
class Sale:
    task_name: str
    task_id: bytes
    dot_per_period: int  # X, planks
    max_jamkb_per_period: int  # Y, planks
    floor: Decimal  # DOT per JAMKB, X / Y
    period: int  # relay blocks
    count: int  # the number of runs; the scheduler stops after the last
    priority: int
    after: int  # relay blocks after enactment


@dataclass
class Config:
    raw: dict
    rpc: str
    tested_spec_version: int
    genesis_hash: str
    asset_id: int
    asset_name: str
    asset_symbol: str
    cap: int
    custody: str
    seller: str
    sink: str
    pool: str
    treasury: str
    custody_dot: int
    seed_dot: int
    seed_jamkb: int
    seed_min_dot: int
    seed_min_jamkb: int
    seller_dot_extra: int
    tranche_jamkb: int
    topup_jamkb: int
    sale: Sale
    use_pallet_jamkb: bool


def load(path: str) -> Config:
    with open(path) as f:
        c = json.load(f)
    seed = c["account_seed"]
    s = c["sale"]
    x = planks(s["dot_per_period"], DOT_DECIMALS)
    floor = Decimal(s["floor_dot_per_jamkb"])
    if floor <= 0:
        raise ValueError("sale.floor_dot_per_jamkb must be above zero")
    # Rounded down, so the effective floor X / Y is never below the stated one.
    y_jamkb = (Decimal(s["dot_per_period"]) / floor).quantize(
        Decimal(1) / 10**JAMKB_DECIMALS, rounding=ROUND_DOWN)
    sale = Sale(
        task_name=s["task_name"],
        task_id=task_id(s["task_name"]),
        dot_per_period=x,
        max_jamkb_per_period=planks(str(y_jamkb), JAMKB_DECIMALS),
        floor=floor,
        period=int(s["period_relay_blocks"]),
        count=int(s["count"]),
        priority=int(s["priority"]),
        after=int(s["after_relay_blocks"]),
    )
    a = c["asset"]
    cfg = Config(
        raw=c,
        rpc=c["rpc"],
        tested_spec_version=int(c["tested_spec_version"]),
        genesis_hash=c["genesis_hash"],
        asset_id=int(a["id"]),
        asset_name=a["name"],
        asset_symbol=a["symbol"],
        cap=planks(a["cap_jamkb"], JAMKB_DECIMALS),
        custody=addr(keyless_account_id(seed)),
        seller=addr(keyless_account_id(seed, "sell")),
        sink=addr(keyless_account_id(seed, "sink")),
        pool=addr(pool_account_id(int(a["id"]))),
        treasury=addr(treasury_account_id()),
        custody_dot=planks(c["custody_dot"], DOT_DECIMALS),
        seed_dot=planks(c["seed"]["dot"], DOT_DECIMALS),
        seed_jamkb=planks(c["seed"]["jamkb"], JAMKB_DECIMALS),
        seed_min_dot=planks(c["seed"]["min_dot"], DOT_DECIMALS),
        seed_min_jamkb=planks(c["seed"]["min_jamkb"], JAMKB_DECIMALS),
        seller_dot_extra=planks(c["seller_dot_extra"], DOT_DECIMALS),
        tranche_jamkb=planks(c["tranche_jamkb"], JAMKB_DECIMALS),
        topup_jamkb=planks(c["topup_jamkb"], JAMKB_DECIMALS),
        sale=sale,
        use_pallet_jamkb=bool(c.get("use_pallet_jamkb", False)),
    )
    validate(cfg, seed)
    return cfg


def validate(cfg: Config, seed: str):
    """Rejects a config that would build a proposal that cannot work."""
    s = cfg.sale
    problems = []
    if seed in KNOWN_PALLET_IDS:
        problems.append(f"account_seed {seed!r} is the PalletId of a pallet")
    for name in ("cap", "custody_dot", "seed_dot", "seed_jamkb", "seed_min_dot",
                 "seed_min_jamkb", "seller_dot_extra", "tranche_jamkb", "topup_jamkb"):
        if getattr(cfg, name) <= 0:
            problems.append(f"{name} must be above zero")
    if cfg.seed_min_dot > cfg.seed_dot or cfg.seed_min_jamkb > cfg.seed_jamkb:
        problems.append("seed minimums must not exceed the seed")
    if s.dot_per_period <= 0 or s.max_jamkb_per_period <= 0:
        problems.append("dot_per_period and its Y must be above zero")
    if s.period <= 0:
        problems.append("period_relay_blocks must be above zero")
    if s.count < 2:
        problems.append("count must be at least 2; the scheduler runs a count of 0 or 1 once")
    if not 0 <= s.priority <= 255:
        problems.append("priority must be 0..255")
    if s.after < 0:
        problems.append("after_relay_blocks must not be negative")
    if problems:
        raise ValueError("invalid config: " + "; ".join(problems))


def fmt_dot(v: int) -> str:
    return f"{Decimal(v) / 10**DOT_DECIMALS:,f} DOT"


def fmt_jamkb(v: int) -> str:
    return f"{Decimal(v) / 10**JAMKB_DECIMALS:,f} JAMKB"
