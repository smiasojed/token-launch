#!/usr/bin/env python3
"""Builds the proposal of one referendum and checks it against the chain. Read-only.

    python build.py --config config/polkadot.json accounts
    python build.py --config config/polkadot.json create
    python build.py --config config/polkadot.json launch [--rpc ws://127.0.0.1:8000] [--strict]
    python build.py --config config/polkadot.json verify out/launch.json

A step writes out/<step>.json: the call data, its preimage hash and length, the runtime it was
built against, and the decoded call. `verify` decodes a built proposal against the chain's
current runtime and rebuilds it; run it before the decision deposit and again before
enactment, since a runtime upgrade can change what the bytes mean.

Warnings do not stop the build; with --strict they make it exit with status 2.
"""

import argparse
import json
import os
import sys
from dataclasses import replace
from decimal import Decimal

from scalecodec import ScaleBytes

from jamkb_trickle.calls import STEPS, Calls
from jamkb_trickle.chain import (
    asset_details,
    connect,
    dot_free,
    jamkb_balance,
    lp_balance,
    lp_token,
    pool_balances,
    price,
    spec_version,
    task_lookup,
)
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load
from jamkb_trickle.encoding import DOT_DECIMALS, blake2_256

INLINE_LIMIT = 128  # bytes; a longer call is stored as a preimage
DOT = 10**DOT_DECIMALS
UNWIND_KEEP = 10_000  # unwind leaves 1/UNWIND_KEEP of the pool share in the pool


def show_accounts(cfg):
    print("Accounts (SS58 prefix 0):")
    for name in ["treasury", "custody", "seller", "sink", "pool"]:
        print(f"  {name:9} {getattr(cfg, name)}")
    s = cfg.sale
    days = Decimal(s.period * s.count * 6) / 86400
    print(f"Sale task id  0x{s.task_id.hex()}  ({s.task_name})")
    print(f"Sale: X = {fmt_dot(s.dot_per_period)} burned per period, "
          f"Y = {fmt_jamkb(s.max_jamkb_per_period)} at most, floor {s.floor} DOT per JAMKB, "
          f"every {s.period} relay blocks, {s.count} runs (about {days:.0f} days)")


def tree(v):
    """The decoded call as plain JSON, nested calls included."""
    if hasattr(v, "call_module") and hasattr(v, "value"):
        return tree(v.value)
    if isinstance(v, dict):
        return {k: tree(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [tree(x) for x in v]
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    return v


def chain_identity(s, cfg):
    genesis = s.get_block_hash(0)
    if genesis != cfg.genesis_hash:
        sys.exit(f"ERROR: connected to genesis {genesis}, the config is for {cfg.genesis_hash}")
    return genesis, spec_version(s)


def unwind_params(s, cfg):
    lp = lp_token(s, cfg)
    held = lp_balance(s, lp, cfg.pool) if lp is not None else 0
    if not held:
        sys.exit("ERROR: the pool account holds no pool share; nothing to unwind")
    return {"lp_burn": held - held // UNWIND_KEEP}


def preflight(s, cfg, step, spec, params):
    """Warnings, not errors: a referendum enacts weeks later, and the state may change."""
    warn = []
    if spec != cfg.tested_spec_version:
        warn.append(f"runtime spec {spec}, tested {cfg.tested_spec_version}: rerun the simulation")
    ed = s.get_constant("Balances", "ExistentialDeposit").value
    fee = s.get_constant("AssetConversion", "PoolSetupFee").value
    touch = (s.get_constant("Assets", "AssetAccountDeposit").value +
             s.get_constant("PoolAssets", "AssetAccountDeposit").value)
    per_block = s.get_constant("Scheduler", "MaxScheduledPerBlock").value
    next_id = s.query("Assets", "NextAssetId").value
    print(f"Chain: spec {spec}, ED {fmt_dot(ed)}, pool setup fee {fmt_dot(fee)}, pool account "
          f"deposits {fmt_dot(touch)}, max scheduled per block {per_block}, next asset id {next_id}")

    asset = asset_details(s, cfg)
    if step == "create":
        if asset:
            warn.append(f"asset {cfg.asset_id} exists already: force_create will fail")
        if next_id is not None and cfg.asset_id >= next_id:
            warn.append(f"asset id {cfg.asset_id} is at or above NextAssetId {next_id}: a signed "
                        "account can create it before the referendum enacts; pick an id below it")
        if cfg.custody_dot < ed:
            warn.append("custody_dot is below the existential deposit")
        if dot_free(s, cfg.treasury) < cfg.custody_dot:
            warn.append("the treasury cannot pay custody_dot")
    elif not asset:
        warn.append(f"asset {cfg.asset_id} does not exist yet: enact `create` first")
    elif {asset["owner"], asset["issuer"], asset["admin"], asset["freezer"]} != {cfg.custody}:
        warn.append("custody does not hold all four asset roles")

    x, y = cfg.sale.dot_per_period, cfg.sale.max_jamkb_per_period
    pool_dot, pool_jamkb = pool_balances(s, cfg)
    pool_exists = lp_token(s, cfg) is not None

    if step in ("seed", "launch"):
        need = cfg.seed_dot + cfg.seller_dot_extra
        if dot_free(s, cfg.treasury) < need:
            warn.append(f"the treasury holds less than {fmt_dot(need)}")
        if cfg.seller_dot_extra < fee + touch + ed:
            warn.append(f"seller_dot_extra is below {fmt_dot(fee + touch + ed)} (pool setup fee, "
                        "the two pool account deposits and the existential deposit)")
        if asset and jamkb_balance(s, cfg, cfg.custody) < cfg.seed_jamkb + cfg.tranche_jamkb:
            warn.append("custody holds less JAMKB than seed + tranche")
        if pool_exists:
            print("note: the pool exists already; create_pool will fail inside dispatch_as, "
                  "which the launch tolerates")
        if pool_dot or pool_jamkb:
            warn.append(f"the pool account holds {fmt_dot(pool_dot)} and {fmt_jamkb(pool_jamkb)}: "
                        "the opening price moves, and add_liquidity at the seed minimums fails "
                        "unless the ratio matches")
        if dot_free(s, cfg.sink):
            warn.append("the sink holds DOT; it stays there after each burn")

    if step in ("start", "launch", "change"):
        if x < ed:
            warn.append("dot_per_period is below the existential deposit: the swap to the sink fails")
        if step == "launch":
            ref_dot, ref_price = cfg.seed_dot + pool_dot, price(cfg.seed_dot, cfg.seed_jamkb)
            seller_jamkb = cfg.tranche_jamkb
        else:
            ref_dot, ref_price = pool_dot, price(pool_dot, pool_jamkb)
            seller_jamkb = jamkb_balance(s, cfg, cfg.seller)
        if not ref_dot:
            warn.append("the pool holds no DOT: every run fails")
        elif x * 1000 >= ref_dot * 3:
            warn.append(f"each sale is at least 0.3% of the pool's DOT ({fmt_dot(ref_dot)}): "
                        "sandwiching pays off; the share grows as the pool drains")
        if ref_price == ref_price and cfg.sale.floor >= Decimal(str(ref_price)):
            warn.append(f"the floor {cfg.sale.floor} is at or above the price {ref_price:.5f}: "
                        "every run fails until the price rises")
        if seller_jamkb < y:
            warn.append(f"the seller holds {fmt_jamkb(seller_jamkb)}, less than Y")
        scheduled = task_lookup(s, cfg)
        if step in ("start", "launch") and scheduled:
            warn.append("a task with this id is scheduled already: use `change`")
        if step == "change" and not scheduled:
            print("note: no task with this id is scheduled; `change` creates it")

    if step == "topup" and asset and jamkb_balance(s, cfg, cfg.custody) < cfg.topup_jamkb:
        warn.append("custody holds less JAMKB than the top-up")
    if step == "stop" and not task_lookup(s, cfg):
        print("note: no task with this id is scheduled; stop still returns the tranche")
    if step == "unwind":
        if pool_dot < 100 * DOT:
            warn.append("the pool holds under 100 DOT: the rest left in the pool may fall below "
                        "the existential deposit, and remove_liquidity then fails")
        print(f"unwind burns {params['lp_burn']} of the pool share and leaves 1/{UNWIND_KEEP} "
              f"(about {fmt_dot(pool_dot // UNWIND_KEEP)}) in the pool")
    return warn


def build(s, cfg, step, params):
    call = Calls(s, cfg).by_step(step, **params)
    data = bytes(call.data.data)
    out = {
        "step": step,
        "call_data": "0x" + data.hex(),
        "preimage_hash": "0x" + blake2_256(data).hex(),
        "preimage_len": len(data),
        "params": params,
        "call": tree(call),
    }
    if step in ("start", "launch", "change"):
        out["sale_call_len"] = len(bytes(Calls(s, cfg).sale_call().data.data))
    return out


def verify(s, cfg, path):
    with open(path) as f:
        built = json.load(f)
    genesis, spec = chain_identity(s, cfg)
    data = bytes.fromhex(built["call_data"][2:])
    problems = []
    if "0x" + blake2_256(data).hex() != built["preimage_hash"] or len(data) != built["preimage_len"]:
        problems.append("preimage_hash or preimage_len does not match call_data")
    if built.get("spec_version") != spec:
        problems.append(f"built against spec {built.get('spec_version')}, the chain runs {spec}")
    try:
        call = s.create_scale_object("Call", data=ScaleBytes(built["call_data"]))
        call.decode()
    except Exception as e:  # noqa: BLE001 - any decode failure means the bytes are stale
        problems.append(f"call_data does not decode against the current runtime: {e}")
    rebuilt = build(s, cfg, built["step"], built.get("params") or {})
    if rebuilt["call_data"] != built["call_data"]:
        problems.append("a fresh build with this config and the current runtime gives other bytes")
    print(f"verify {path}: {built['step']}, {len(data)} bytes, chain spec {spec}")
    for p in problems:
        print(f"MISMATCH: {p}")
    if problems:
        sys.exit(1)
    print("OK: the bytes decode and equal a fresh build")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("--rpc", help="read the chain from this endpoint instead, e.g. a fork")
    p.add_argument("--strict", action="store_true", help="exit with status 2 on any warning")
    p.add_argument("step", choices=["accounts", "verify"] + STEPS)
    p.add_argument("file", nargs="?", help="out/<step>.json, for verify")
    args = p.parse_args()

    cfg = load(args.config)
    if args.rpc:
        cfg = replace(cfg, rpc=args.rpc)
    show_accounts(cfg)
    if args.step == "accounts":
        return

    s = connect(cfg.rpc)
    if args.step == "verify":
        if not args.file:
            sys.exit("verify needs out/<step>.json")
        verify(s, cfg, args.file)
        return

    genesis, spec = chain_identity(s, cfg)
    params = unwind_params(s, cfg) if args.step == "unwind" else {}
    out = build(s, cfg, args.step, params)
    out["spec_version"], out["genesis_hash"] = spec, genesis
    warnings = preflight(s, cfg, args.step, spec, params)

    os.makedirs("out", exist_ok=True)
    path = f"out/{args.step}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n{args.step}: {out['preimage_len']} bytes, preimage hash {out['preimage_hash']}")
    if "sale_call_len" in out:
        stored = "a preimage" if out["sale_call_len"] > INLINE_LIMIT else "inline"
        print(f"sale call: {out['sale_call_len']} bytes, the scheduler stores it as {stored}")
    print(f"written to {path}")
    for w in warnings:
        print(f"WARNING: {w}")
    if warnings and args.strict:
        sys.exit(2)


if __name__ == "__main__":
    main()
