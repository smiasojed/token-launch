#!/usr/bin/env python3
"""Rehearses every referendum on a chopsticks fork of Asset Hub Polkadot and checks the result.

    ./fork.sh
    python simulate.py --config config/simulation.json

Each referendum is enacted the way an approved Root referendum is: its preimage is noted by a
signed account, and a scheduler agenda entry with the Root origin dispatches it. The proposals
are the ones build.py produces for Polkadot; only the sale period is shorter. Alice stands in
for the market: she creates the pool first, dumps JAMKB and buys it back.

Exits with status 1 if a check fails.
"""

import argparse
import sys
from dataclasses import replace

from substrateinterface import Keypair

from jamkb_trickle.calls import Calls
from jamkb_trickle.chain import (
    connect,
    dot_free,
    jamkb_balance,
    module_error,
    pool_balances,
    price,
    relay_number,
    spec_version,
    task_lookup,
)
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load
from jamkb_trickle.encoding import DOT_DECIMALS, JAMKB_DECIMALS, blake2_256, task_id

DOT, KB = 10**DOT_DECIMALS, 10**JAMKB_DECIMALS
failures = []


def transferred(evs, cfg, src, dst):
    """JAMKB moved from `src` to `dst` in the given events."""
    return sum(a["amount"] for m, n, a in evs if (m, n) == ("Assets", "Transferred")
               and a["asset_id"] == cfg.asset_id and a["from"] == src and a["to"] == dst)


def check(ok, what):
    print(f"    {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        failures.append(what)


class Fork:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = connect(cfg.rpc)
        self.alice = Keypair.create_from_uri("//Alice", ss58_format=0)
        self.head = self.s.get_chain_head()

    def rpc(self, method, params):
        r = self.s.rpc_request(method, params)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r["result"]

    def new_block(self):
        self.head = self.rpc("dev_newBlock", [{"count": 1}])
        return self.events()

    def events(self, block_hash=None):
        out = []
        for e in self.s.get_events(block_hash or self.head):
            ev = e.value["event"]
            out.append((ev["module_id"], ev["event_id"], ev["attributes"]))
        return out

    def q(self, fn, *args):
        return fn(self.s, *args, block_hash=self.head)

    def signed(self, call):
        """Alice sends `call`; returns the events of the block that includes it."""
        ext = self.s.create_signed_extrinsic(call=call, keypair=self.alice)
        self.s.submit_extrinsic(ext, wait_for_inclusion=False)
        return self.new_block()

    def enact(self, label, call, expect_ok=True):
        """Notes the preimage and dispatches `call` with the Root origin, as an enacted
        referendum does. Returns the events of the enactment block."""
        data = bytes(call.data.data)
        h = "0x" + blake2_256(data).hex()
        if not self.s.query("Preimage", "RequestStatusFor", [h], block_hash=self.head).value:
            self.signed(self.s.compose_call("Preimage", "note_preimage", {"bytes": "0x" + data.hex()}))
        # dev_setStorage replaces the whole agenda of a block: take one that is empty and that
        # the next block services (chopsticks moves the relay number by 4 per block).
        relay = relay_number(self.s, self.head)
        at = next(b for b in range(relay + 1, relay + 5)
                  if not self.s.query("Scheduler", "Agenda", [b], block_hash=self.head).value)
        self.rpc("dev_setStorage", [{"Scheduler": {"Agenda": [[[at], [{
            "maybeId": None, "priority": 63, "maybePeriodic": None,
            "origin": {"system": "Root"}, "call": {"Lookup": {"hash": h, "len": len(data)}},
        }]]]}}])
        # The scheduler reads the relay number of the parent block in `on_initialize`, so the
        # entry runs in the first block whose parent's relay number has reached `at`.
        for _ in range(3):
            evs = self.new_block()
            runs = [a for m, n, a in evs
                    if m == "Scheduler" and n == "Dispatched" and a.get("id") is None]
            unavailable = [a for m, n, a in evs if m == "Scheduler" and n == "CallUnavailable"]
            if runs or unavailable:
                break
        result = module_error(self.s, runs[0]["result"]) if runs else (
            "CallUnavailable" if unavailable else "not dispatched")
        print(f"\n[referendum] {label}: {result} ({len(data)} bytes)")
        for m, n, a in evs:
            if (m, n) in {("Utility", "BatchInterrupted"), ("Utility", "ItemFailed"),
                          ("Utility", "BatchCompletedWithErrors"), ("Scheduler", "Scheduled"),
                          ("Scheduler", "Canceled"), ("AssetConversion", "LiquidityAdded")}:
                print(f"    {m}.{n} {a if n != 'LiquidityAdded' else ''}")
            if (m, n) == ("Utility", "DispatchedAs") and "Err" in a["result"]:
                print(f"    Utility.DispatchedAs {module_error(self.s, a['result'])}")
        if expect_ok:
            check(result == "Ok", f"{label} enacted")
        return result, evs

    def next_run(self, cfg, max_blocks=12):
        """Builds blocks until the task runs; returns (result, burned, sold, events)."""
        task = "0x" + cfg.sale.task_id.hex()
        for _ in range(max_blocks):
            k0 = self.q(jamkb_balance, cfg, cfg.seller)
            evs = self.new_block()
            runs = [a for m, n, a in evs
                    if m == "Scheduler" and a.get("id") == task and n != "Scheduled"]
            if not runs:
                continue
            burned = sum(a["amount"] for m, n, a in evs
                         if (m, n) == ("Balances", "Burned") and a["who"] == cfg.sink)
            sold = k0 - self.q(jamkb_balance, cfg, cfg.seller)
            result = module_error(self.s, runs[0]["result"]) if "result" in runs[0] else str(runs[0])
            d, k = self.q(pool_balances, cfg)
            print(f"\n[sale @relay {relay_number(self.s, self.head)}] {result}: burned "
                  f"{fmt_dot(burned)}, sold {fmt_jamkb(sold)}, price {price(d, k):.5f}")
            return result, burned, sold, evs
        return None, 0, 0, []


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/simulation.json")
    p.add_argument("--no-precreate", action="store_true",
                   help="do not create the pool before the launch referendum")
    args = p.parse_args()
    cfg = load(args.config)
    f = Fork(cfg)
    C = Calls(f.s, cfg)
    s = f.s
    spec = spec_version(s, f.head)
    print(f"fork of Asset Hub Polkadot, spec {spec}; custody {cfg.custody}, seller {cfg.seller}, "
          f"sink {cfg.sink}, pool {cfg.pool}")
    if spec != cfg.tested_spec_version:
        print(f"note: runtime spec {spec} differs from the tested {cfg.tested_spec_version}")

    # Alice pays preimage deposits and plays the market.
    f.rpc("dev_setStorage", [{"System": {"Account": [[[f.alice.ss58_address],
          {"providers": 1, "data": {"free": str(1_000_000 * DOT)}}]]}}])

    # ── Referendum A: create JAMKB ─────────────────────────────────────────────────────
    f.enact("A. create JAMKB", C.create_jamkb())
    check(f.q(jamkb_balance, cfg, cfg.custody) == cfg.cap, "the cap is in custody")

    # Someone creates the pool before the launch referendum enacts.
    lp = None
    evs = [] if args.no_precreate else f.signed(s.compose_call("AssetConversion", "create_pool", {
        "asset1": {"parents": 1, "interior": "Here"},
        "asset2": {"parents": 0, "interior": {"X2": [{"PalletInstance": 50},
                                                       {"GeneralIndex": cfg.asset_id}]}}}))
    for m, n, a in evs:
        if (m, n) == ("AssetConversion", "PoolCreated"):
            lp = a["lp_token"]
            check(a["pool_account"] == cfg.pool, "the derived pool account is the chain's")
    if not args.no_precreate:
        check(lp is not None, "Alice created the pool before the launch referendum")

    # ── Referendum B: seed and start ───────────────────────────────────────────────────
    _, evs = f.enact("B. launch (seed + start)", C.launch())
    for m, n, a in evs:
        if (m, n) == ("AssetConversion", "PoolCreated"):
            lp = a["lp_token"]
            check(a["pool_account"] == cfg.pool, "the derived pool account is the chain's")
    check(lp is not None, "the pool exists after the launch")
    d, k = f.q(pool_balances, cfg)
    check(k == cfg.seed_jamkb and d >= cfg.seed_dot, f"pool seeded: {fmt_dot(d)} / {fmt_jamkb(k)}")
    lp_pool = s.query("PoolAssets", "Account", [lp, cfg.pool], block_hash=f.head).value
    check(bool(lp_pool) and lp_pool["balance"] > 0, "the pool share is held by the pool account")
    check(f.q(jamkb_balance, cfg, cfg.seller) == cfg.tranche_jamkb, "the seller holds the tranche")
    check(task_lookup(s, cfg, f.head) is not None, "the sale is scheduled")

    x, y = cfg.sale.dot_per_period, cfg.sale.max_jamkb_per_period
    for i in (1, 2):
        r, burned, sold, _ = f.next_run(cfg)
        check(r == "Ok" and burned == x and 0 < sold <= y,
              f"period {i}: burned exactly X, sold at most Y")
        check(f.q(dot_free, cfg.sink) == 0, f"period {i}: the sink is reaped")

    # ── Referendum C: change the parameters under the same task id ─────────────────────
    cfg_c = replace(cfg, sale=replace(cfg.sale, dot_per_period=100 * DOT,
                                      max_jamkb_per_period=1250 * KB))
    f.enact("C. change X to 100 DOT", Calls(s, cfg_c).change())
    r, burned, _, _ = f.next_run(cfg_c)
    check(r == "Ok" and burned == 100 * DOT, "after the change each period burns the new X")

    # The market dumps below the floor.
    f.enact("holder grant to Alice (market stand-in)", C.grant(f.alice.ss58_address, 2_000_000 * KB))
    jam = {"parents": 0, "interior": {"X2": [{"PalletInstance": 50}, {"GeneralIndex": cfg.asset_id}]}}
    dot = {"parents": 1, "interior": "Here"}
    f.signed(s.compose_call("AssetConversion", "swap_exact_tokens_for_tokens", {
        "path": [jam, dot], "amount_in": 1_500_000 * KB, "amount_out_min": 1,
        "send_to": f.alice.ss58_address, "keep_alive": True}))
    d, k = f.q(pool_balances, cfg)
    print(f"\nAlice dumped JAMKB: price {price(d, k):.5f}, floor {cfg_c.sale.floor}")
    r, burned, sold, _ = f.next_run(cfg_c)
    check(r == "AssetConversion.ProvidedMaximumNotSufficientForSwap" and burned == 0 and sold == 0,
          "below the floor the sale fails and nothing is burned")
    check(task_lookup(s, cfg, f.head) is not None, "the task stays scheduled after a failed period")

    # The market buys back.
    f.signed(s.compose_call("AssetConversion", "swap_tokens_for_exact_tokens", {
        "path": [dot, jam], "amount_out": 1_550_000 * KB, "amount_in_max": 1_000_000 * DOT,
        "send_to": f.alice.ss58_address, "keep_alive": True}))
    d, k = f.q(pool_balances, cfg)
    print(f"\nAlice bought back: price {price(d, k):.5f}")
    r, burned, _, _ = f.next_run(cfg_c)
    check(r == "Ok" and burned == 100 * DOT, "above the floor the sale resumes")

    # ── Referendum D: top up ───────────────────────────────────────────────────────────
    _, evs = f.enact("D. top up", C.topup())
    check(transferred(evs, cfg, cfg.custody, cfg.seller) == cfg.topup_jamkb, "the seller got the top-up")

    # ── Referendum E: stop and return ──────────────────────────────────────────────────
    _, evs = f.enact("E. stop", C.stop())
    check(task_lookup(s, cfg, f.head) is None, "the task is cancelled")
    back = transferred(evs, cfg, cfg.seller, cfg.custody)
    check(back > 0 and f.q(jamkb_balance, cfg, cfg.seller) == 0,
          f"the unsold {fmt_jamkb(back)} returned to custody")
    r, _, _, _ = f.next_run(cfg, max_blocks=6)
    check(r is None, "no sale after the stop")

    # ── count semantics, and a stop once the task is gone ──────────────────────────────
    cfg_n = replace(cfg, sale=replace(cfg.sale, task_name="jamkb-count-test",
                                      task_id=task_id("jamkb-count-test"), count=2))
    Cn = Calls(s, cfg_n)
    f.enact("F. top up and start a sale with count 2", Cn.batch_all([Cn.topup(), Cn.start()]))
    runs = 0
    while f.next_run(cfg_n)[0] is not None:
        runs += 1
    check(runs == 2, f"count 2 runs the sale exactly 2 times (ran {runs})")
    check(task_lookup(s, cfg_n, f.head) is None, "the task is gone after its last run")

    held = f.q(jamkb_balance, cfg, cfg.seller)
    r, _ = f.enact("G. stop in batch_all once the task is gone", Cn.stop_batch_all(), expect_ok=False)
    check(r == "Scheduler.NotFound" and f.q(jamkb_balance, cfg, cfg.seller) == held,
          "batch_all fails the whole referendum: nothing returned")
    f.enact("H. stop in force_batch once the task is gone", Cn.stop())
    check(f.q(jamkb_balance, cfg, cfg.seller) == 0, "force_batch still returns the tranche")

    print(f"\ncustody {fmt_jamkb(f.q(jamkb_balance, cfg, cfg.custody))}, "
          f"pool {fmt_dot(f.q(pool_balances, cfg)[0])} / {fmt_jamkb(f.q(pool_balances, cfg)[1])}")
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
    for w in failures:
        print(f"  FAILED: {w}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
