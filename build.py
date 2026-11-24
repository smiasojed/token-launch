#!/usr/bin/env python3
"""Builds the proposal of one referendum and checks it against the chain. Read-only.

    python build.py --config config/polkadot.json accounts
    python build.py --config config/polkadot.json create
    python build.py --config config/polkadot.json launch

Writes out/<step>.json with the call data, its preimage hash and length, and the decoded call.
"""

import argparse
import json
import os
from dataclasses import replace

from jamkb_trickle.calls import STEPS, Calls
from jamkb_trickle.chain import (
    asset_details,
    connect,
    dot_free,
    jamkb_balance,
    pool_balances,
    price,
    spec_version,
    task_lookup,
)
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load
from jamkb_trickle.encoding import blake2_256

INLINE_LIMIT = 128  # bytes; a longer call is stored as a preimage


def show_accounts(cfg):
    print("Accounts (SS58 prefix 0):")
    for name in ["treasury", "custody", "seller", "sink", "pool"]:
        print(f"  {name:9} {getattr(cfg, name)}")
    print(f"Sale task id  0x{cfg.sale.task_id.hex()}  ({cfg.sale.task_name})")
    s = cfg.sale
    print(f"Sale: X = {fmt_dot(s.dot_per_period)} burned per period, "
          f"Y = {fmt_jamkb(s.max_jamkb_per_period)} at most, floor {s.floor} DOT per JAMKB, "
          f"every {s.period} relay blocks, {s.count} runs")


def preflight(s, cfg, step):
    """Warnings, not errors: a referendum enacts later, the state may change by then."""
    warn = []
    spec = spec_version(s)
    if spec != cfg.tested_spec_version:
        warn.append(f"runtime spec {spec}, tested {cfg.tested_spec_version}: rerun the simulation")
    ed = s.get_constant("Balances", "ExistentialDeposit").value
    fee = s.get_constant("AssetConversion", "PoolSetupFee").value
    per_block = s.get_constant("Scheduler", "MaxScheduledPerBlock").value
    print(f"Chain: spec {spec}, ED {fmt_dot(ed)}, pool setup fee {fmt_dot(fee)}, "
          f"max scheduled per block {per_block}")

    asset = asset_details(s, cfg)
    if step == "create":
        if asset:
            warn.append(f"asset {cfg.asset_id} exists already: force_create will fail")
        if cfg.custody_dot < ed:
            warn.append("custody_dot is below the existential deposit")
    elif not asset:
        warn.append(f"asset {cfg.asset_id} does not exist yet: enact `create` first")
    elif asset["owner"] != cfg.custody:
        warn.append(f"asset owner {asset['owner']} is not custody {cfg.custody}")

    if step in ("seed", "launch"):
        need = cfg.seed_dot + cfg.seller_dot_extra
        have = dot_free(s, cfg.treasury)
        if have < need:
            warn.append(f"treasury holds {fmt_dot(have)}, needs {fmt_dot(need)}")
        if cfg.seller_dot_extra < fee + ed:
            warn.append("seller_dot_extra is below pool setup fee + existential deposit")
        if asset and jamkb_balance(s, cfg, cfg.custody) < cfg.seed_jamkb + cfg.tranche_jamkb:
            warn.append("custody holds less JAMKB than seed + tranche")
        dot, jamkb = pool_balances(s, cfg)
        if jamkb:
            warn.append(f"the pool holds liquidity already (price {price(dot, jamkb):.5f}): "
                        "add_liquidity with the seed minimums will fail unless the ratio matches")
        if dot_free(s, cfg.sink):
            warn.append("the sink holds DOT; it stays there after each burn")

    if step in ("start", "launch", "change"):
        x, y = cfg.sale.dot_per_period, cfg.sale.max_jamkb_per_period
        if x < ed:
            warn.append("dot_per_period is below the existential deposit: the swap to the sink fails")
        dot, _ = pool_balances(s, cfg)
        pool_dot = dot if dot else cfg.seed_dot
        if x * 1000 > pool_dot * 3:
            warn.append("each sale is above 0.3% of the pool's DOT: sandwiching pays off")
        if step in ("start", "launch") and task_lookup(s, cfg):
            warn.append("a task with this id is scheduled already: use `change`")
    if step == "stop" and not task_lookup(s, cfg):
        print("note: no task with this id is scheduled; stop still returns the tranche")
    return warn


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("step", choices=["accounts"] + STEPS)
    p.add_argument("--rpc", help="read the chain from this endpoint instead, e.g. a fork")
    args = p.parse_args()

    cfg = load(args.config)
    if args.rpc:
        cfg = replace(cfg, rpc=args.rpc)
    show_accounts(cfg)
    if args.step == "accounts":
        return

    s = connect(cfg.rpc)
    call = Calls(s, cfg).by_step(args.step)
    data = bytes(call.data.data)
    out = {
        "step": args.step,
        "call_data": "0x" + data.hex(),
        "preimage_hash": "0x" + blake2_256(data).hex(),
        "preimage_len": len(data),
        "call": call.value,
    }
    if args.step in ("start", "launch", "change"):
        sale = Calls(s, cfg).sale_call()
        out["sale_call_len"] = len(bytes(sale.data.data))

    warnings = preflight(s, cfg, args.step)
    os.makedirs("out", exist_ok=True)
    path = f"out/{args.step}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\n{args.step}: {out['preimage_len']} bytes, preimage hash {out['preimage_hash']}")
    if "sale_call_len" in out:
        stored = "a preimage" if out["sale_call_len"] > INLINE_LIMIT else "inline"
        print(f"sale call: {out['sale_call_len']} bytes, the scheduler stores it as {stored}")
    print(f"written to {path}")
    for w in warnings:
        print(f"WARNING: {w}")


if __name__ == "__main__":
    main()
