"""Loads a JSON config and derives every account, amount and id the referenda use."""

import json
from dataclasses import dataclass
from decimal import Decimal

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
    y_jamkb = (Decimal(s["dot_per_period"]) / floor).quantize(Decimal(1) / 10**JAMKB_DECIMALS)
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
    return Config(
        raw=c,
        rpc=c["rpc"],
        tested_spec_version=int(c["tested_spec_version"]),
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


def fmt_dot(v: int) -> str:
    return f"{Decimal(v) / 10**DOT_DECIMALS:,f} DOT"


def fmt_jamkb(v: int) -> str:
    return f"{Decimal(v) / 10**JAMKB_DECIMALS:,f} JAMKB"
