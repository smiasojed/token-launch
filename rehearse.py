#!/usr/bin/env python3
"""Enacts the exact proposal of out/<step>.json on a chopsticks fork, as an approved Root
referendum would, and checks the state it leaves. Run it on every proposal before it is
submitted.

    ./fork.sh
    python build.py    --config config/polkadot.json create          # bytes built against Polkadot
    python rehearse.py --config config/polkadot.json out/create.json
    python build.py    --config config/polkadot.json launch --rpc ws://127.0.0.1:8000
    python rehearse.py --config config/polkadot.json out/launch.json

For `start`, `launch` and `change` it also builds blocks until the first sale runs, and checks
that it burned exactly X. The fork keeps its state between runs, so enact the steps in order.
Restart it with ./fork.sh to start again from the live chain.

Exits with status 1 if the referendum fails, if an item inside it failed (other than a cancel
of a task that is already gone, or a pool that exists already), or if a check fails.
"""

import argparse
import json
import sys
from dataclasses import replace

from scalecodec import ScaleBytes

import simulate
from jamkb_trickle.chain import (
    asset_details,
    dot_free,
    jamkb_balance,
    lp_balance,
    lp_supply,
    lp_token,
    pool_balances,
    price,
    task_lookup,
)
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load
from simulate import Fork, check, transferred


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("--rpc", default="ws://127.0.0.1:8000", help="the fork")
    p.add_argument("proposal", help="out/<step>.json written by build.py")
    args = p.parse_args()

    cfg = replace(load(args.config), rpc=args.rpc)
    with open(args.proposal) as f:
        built = json.load(f)
    step = built["step"]
    f = Fork(cfg)
    if f.s.get_block_hash(0) != cfg.genesis_hash:
        sys.exit("ERROR: the fork is not of the chain in the config")
    f.fund_alice(1_000)
    call = f.s.create_scale_object("Call", data=ScaleBytes(built["call_data"]))
    call.decode()
    seller0 = f.q(jamkb_balance, cfg, cfg.seller)
    _, _, evs = f.enact(step, call)
    s = f.s

    if step == "create":
        a = f.q(asset_details, cfg)
        check(f.q(jamkb_balance, cfg, cfg.custody) == cfg.cap, "the cap is in custody")
        check({a["owner"], a["issuer"], a["admin"], a["freezer"]} == {cfg.custody},
              "custody holds all four asset roles")
    if step in ("seed", "launch"):
        lp = f.q(lp_token, cfg)
        d, k = f.q(pool_balances, cfg)
        check(k == cfg.seed_jamkb and d >= cfg.seed_dot, f"pool seeded: {fmt_dot(d)} / {fmt_jamkb(k)}")
        check(lp is not None and f.q(lp_balance, lp, cfg.pool) == f.q(lp_supply, lp),
              "the whole pool share is held by the pool account")
        check(f.q(jamkb_balance, cfg, cfg.seller) == cfg.tranche_jamkb, "the seller holds the tranche")
    if step in ("start", "launch", "change"):
        check(f.q(task_lookup, cfg) is not None, "the sale is scheduled")
        r = f.next_run(cfg, max_blocks=6)
        check(r is not None and r["result"] == "Ok" and r["burned"] == cfg.sale.dot_per_period,
              "the first sale ran and burned exactly X")
    if step == "topup":
        check(transferred(evs, cfg, cfg.custody, cfg.seller) == cfg.topup_jamkb,
              "the seller received the top-up")
    if step == "stop":
        check(f.q(task_lookup, cfg) is None, "the sale is not scheduled")
        check(f.q(jamkb_balance, cfg, cfg.seller) == 0 and
              transferred(evs, cfg, cfg.seller, cfg.custody) == seller0,
              "the seller's JAMKB returned to custody")
    if step == "unwind":
        check(f.q(task_lookup, cfg) is None, "the sale is not scheduled")
        ed = s.get_constant("Balances", "ExistentialDeposit").value
        check(f.q(dot_free, cfg.seller) <= ed and f.q(jamkb_balance, cfg, cfg.seller) == 0,
              "the seller is emptied: JAMKB to custody, free DOT to the treasury")

    h = f.head
    d, k = pool_balances(s, cfg, h)
    lookup = task_lookup(s, cfg, h)
    print(f"\ncustody {fmt_jamkb(jamkb_balance(s, cfg, cfg.custody, h))}, "
          f"seller {fmt_jamkb(jamkb_balance(s, cfg, cfg.seller, h))} and "
          f"{fmt_dot(dot_free(s, cfg.seller, h))}, pool {fmt_dot(d)} / {fmt_jamkb(k)} "
          f"(price {price(d, k):.5f}), sale task "
          f"{'next run at relay ' + str(lookup[0]) if lookup else 'not scheduled'}")
    if simulate.failures:
        print(f"{len(simulate.failures)} CHECK(S) FAILED")
        sys.exit(1)
    print("rehearsal passed")


if __name__ == "__main__":
    main()
