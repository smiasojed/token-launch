#!/usr/bin/env python3
"""Watches the sale: every run of the task, its burn, the price and what is left to sell.

    python monitor.py --config config/polkadot.json

Prints an ALERT line when the scheduler fails the task for good (PeriodicFailed,
CallUnavailable, PermanentlyOverweight) or when the task is no longer scheduled. Those stop the
sale silently; only a new Root referendum restarts it.
"""

import argparse
import time

from jamkb_trickle.chain import (
    connect,
    jamkb_balance,
    module_error,
    pool_balances,
    price,
    task_lookup,
)
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load

FATAL = {"PeriodicFailed", "CallUnavailable", "PermanentlyOverweight"}


def scan_block(s, cfg, block_hash, number, state):
    task = "0x" + cfg.sale.task_id.hex()
    scheduled = task_lookup(s, cfg, block_hash) is not None
    if state.get("scheduled") and not scheduled:
        print(f"#{number}: ALERT the task is no longer scheduled (cancelled, last run done, "
              "or dropped by the scheduler)")
    state["scheduled"] = scheduled
    burned, lines = 0, []
    for e in s.get_events(block_hash):
        ev = e.value["event"]
        mod, name, attrs = ev["module_id"], ev["event_id"], ev["attributes"]
        if mod == "Scheduler" and isinstance(attrs, dict) and attrs.get("id") == task:
            if name == "Dispatched":
                lines.append(f"run: {module_error(s, attrs['result'])}")
            elif name in FATAL:
                lines.append(f"ALERT {name}: the sale stopped, a referendum must restart it")
            else:
                lines.append(f"{name}")
        if mod == "Balances" and name == "Burned" and attrs.get("who") == cfg.sink:
            burned += attrs["amount"]
    if not lines:
        return
    dot, jamkb = pool_balances(s, cfg, block_hash)
    left = jamkb_balance(s, cfg, cfg.seller, block_hash)
    nxt = task_lookup(s, cfg, block_hash)
    print(f"#{number}: {'; '.join(lines)}; burned {fmt_dot(burned)}; price {price(dot, jamkb):.5f}; "
          f"seller holds {fmt_jamkb(left)}; next run {nxt[0] if nxt else 'NONE'}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("--from-block", type=int, help="scan from this block (default: the head)")
    args = p.parse_args()
    cfg = load(args.config)
    s = connect(cfg.rpc)
    head = s.get_block_number(s.get_chain_head())
    n = args.from_block or head
    state = {}
    nxt = task_lookup(s, cfg)
    print(f"watching task {cfg.sale.task_name} from #{n}; next run at relay block "
          f"{nxt[0] if nxt else 'NONE'}")
    while True:
        head = s.get_block_number(s.get_chain_head())
        while n <= head:
            scan_block(s, cfg, s.get_block_hash(n), n, state)
            n += 1
        time.sleep(6)


if __name__ == "__main__":
    main()
