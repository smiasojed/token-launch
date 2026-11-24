#!/usr/bin/env python3
"""Watches the sale on finalized blocks: every run, its burn, the price and what is left.

    python monitor.py --config config/polkadot.json [--webhook URL] [--from-block N]

Prints an ALERT line, and posts it to --webhook as JSON {"text": ...}, when:
  - the task is not scheduled (at start, or when it disappears: cancelled, last run done, or
    dropped by the scheduler);
  - the scheduler fails it for good (PeriodicFailed, CallUnavailable, PermanentlyOverweight);
  - a run is overdue: the relay chain passed its slot and it did not run;
  - --failures runs in a row failed (sold out, below the floor);
  - the agenda of its next slot is filling up: at 50 entries the scheduler drops the task.
Reconnects after a dropped connection. Exits only on Ctrl-C, or at --to-block.
"""

import argparse
import json
import time
import urllib.request

from jamkb_trickle.chain import (
    agenda_len,
    connect,
    jamkb_balance,
    module_error,
    pool_balances,
    price,
    relay_number,
    task_lookup,
)
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load

FATAL = {"PeriodicFailed", "CallUnavailable", "PermanentlyOverweight"}
OVERDUE_GRACE = 10  # relay blocks
AGENDA_WARN = 40  # of MaxScheduledPerBlock = 50


class Monitor:
    def __init__(self, cfg, webhook, failures_alert):
        self.cfg, self.webhook, self.failures_alert = cfg, webhook, failures_alert
        self.task = "0x" + cfg.sale.task_id.hex()
        self.scheduled = None
        self.failed_in_row = 0
        self.alerted = set()

    def alert(self, number, text):
        line = f"#{number}: ALERT {text}"
        print(line, flush=True)
        if self.webhook:
            try:
                req = urllib.request.Request(self.webhook, json.dumps({"text": line}).encode(),
                                             {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:  # noqa: BLE001 - the alert is printed anyway
                print(f"webhook failed: {e}", flush=True)

    def once(self, key, number, text):
        if key not in self.alerted:
            self.alerted.add(key)
            self.alert(number, text)

    def scan(self, s, block_hash, number):
        cfg = self.cfg
        nxt = task_lookup(s, cfg, block_hash)
        if nxt is None and self.scheduled is not False:
            self.alert(number, "the task is not scheduled (cancelled, last run done, or dropped "
                               "by the scheduler): only a referendum restarts the sale")
        self.scheduled = nxt is not None
        if nxt is not None:
            relay = relay_number(s, block_hash)
            if relay > nxt[0] + OVERDUE_GRACE:
                self.once(("overdue", nxt[0]), number,
                          f"the run due at relay block {nxt[0]} is overdue (relay now {relay})")
            fill = agenda_len(s, nxt[0], block_hash)
            if fill >= AGENDA_WARN:
                self.once(("agenda", nxt[0]), number,
                          f"the agenda of the next slot {nxt[0]} holds {fill} of 50 entries; "
                          "when the following slot is full the scheduler drops the task")

        burned, lines = 0, []
        for e in s.get_events(block_hash):
            ev = e.value["event"]
            mod, name, attrs = ev["module_id"], ev["event_id"], ev["attributes"]
            if mod == "Scheduler" and isinstance(attrs, dict) and attrs.get("id") == self.task:
                if name == "Dispatched":
                    result = module_error(s, attrs["result"])
                    lines.append(f"run: {result}")
                    self.failed_in_row = 0 if result == "Ok" else self.failed_in_row + 1
                    if self.failed_in_row >= self.failures_alert:
                        self.alert(number, f"{self.failed_in_row} runs in a row failed ({result})")
                elif name in FATAL:
                    self.alert(number, f"{name}: the scheduler dropped the sale")
            if mod == "Balances" and name == "Burned" and attrs.get("who") == cfg.sink:
                burned += attrs["amount"]
        if lines:
            dot, jamkb = pool_balances(s, cfg, block_hash)
            left = jamkb_balance(s, cfg, cfg.seller, block_hash)
            print(f"#{number}: {'; '.join(lines)}; burned {fmt_dot(burned)}; pool price "
                  f"{price(dot, jamkb):.5f}; seller holds {fmt_jamkb(left)}; next run "
                  f"{nxt[0] if nxt else 'NONE'}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("--from-block", type=int, help="scan from this block (default: finalized head)")
    p.add_argument("--to-block", type=int, help="stop after this block")
    p.add_argument("--webhook", help="POST alerts here as JSON {\"text\": ...}")
    p.add_argument("--failures", type=int, default=3, help="alert after this many failed runs in a row")
    p.add_argument("--best", action="store_true", help="follow the best head, not the finalized one "
                   "(for a fork that does not finalize)")
    args = p.parse_args()
    cfg = load(args.config)
    m = Monitor(cfg, args.webhook, args.failures)
    n = args.from_block
    print(f"watching task {cfg.sale.task_name} (0x{cfg.sale.task_id.hex()})", flush=True)
    while True:
        try:
            s = connect(cfg.rpc)
            while True:
                head_hash = s.get_chain_head() if args.best else s.get_chain_finalised_head()
                head = s.get_block_number(head_hash)
                if n is None:
                    n = head
                while n <= head:
                    m.scan(s, s.get_block_hash(n), n)
                    if args.to_block is not None and n >= args.to_block:
                        return
                    n += 1
                time.sleep(6)
        except KeyboardInterrupt:
            return
        except Exception as e:  # noqa: BLE001 - reconnect on any connection error
            print(f"connection lost ({e}); reconnecting in 10 s", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
