#!/usr/bin/env python3
"""Enacts the exact proposal of out/<step>.json on a chopsticks fork, as an approved Root
referendum would, and prints what happened. Run it on every proposal before it is submitted.

    ./fork.sh
    python build.py    --config config/polkadot.json create          # bytes built against Polkadot
    python rehearse.py --config config/polkadot.json out/create.json
    python build.py    --config config/polkadot.json launch --rpc ws://127.0.0.1:8000
    python rehearse.py --config config/polkadot.json out/launch.json

The fork keeps its state between runs, so enact the steps in order. Restart it with ./fork.sh
to start again from the live chain.
"""

import argparse
import json
import sys
from dataclasses import replace

from scalecodec import ScaleBytes

from jamkb_trickle.chain import dot_free, jamkb_balance, pool_balances, price, task_lookup
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load
from simulate import Fork


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("--rpc", default="ws://127.0.0.1:8000", help="the fork")
    p.add_argument("proposal", help="out/<step>.json written by build.py")
    p.add_argument("--blocks", type=int, default=0, help="blocks to build after enactment")
    args = p.parse_args()

    cfg = replace(load(args.config), rpc=args.rpc)
    with open(args.proposal) as f:
        built = json.load(f)
    f = Fork(cfg)
    f.rpc("dev_setStorage", [{"System": {"Account": [[[f.alice.ss58_address],
          {"providers": 1, "data": {"free": str(1_000 * 10**10)}}]]}}])
    call = f.s.create_scale_object("Call", data=ScaleBytes(built["call_data"]))
    call.decode()
    result, evs = f.enact(built["step"], call)
    for m, n, a in evs:
        print(f"    {m}.{n}")
    for _ in range(args.blocks):
        f.new_block()
    s, h = f.s, f.head
    d, k = pool_balances(s, cfg, h)
    lookup = task_lookup(s, cfg, h)
    print(f"\ncustody {fmt_jamkb(jamkb_balance(s, cfg, cfg.custody, h))}, "
          f"seller {fmt_jamkb(jamkb_balance(s, cfg, cfg.seller, h))} and {fmt_dot(dot_free(s, cfg.seller, h))}, "
          f"pool {fmt_dot(d)} / {fmt_jamkb(k)} (price {price(d, k):.5f}), "
          f"sale task {'next run at relay ' + str(lookup[0]) if lookup else 'not scheduled'}")
    sys.exit(0 if result == "Ok" else 1)


if __name__ == "__main__":
    main()
