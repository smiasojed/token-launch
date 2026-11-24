#!/usr/bin/env python3
"""Rehearses every referendum on a chopsticks fork of Asset Hub Polkadot and checks the result.

    ./fork.sh && python simulate.py --config config/simulation.json
    ./fork.sh && python simulate.py --config config/simulation.json --scenario no-precreate
    ./fork.sh && python simulate.py --config config/simulation.json --scenario liquidity-first

Each referendum is enacted the way an approved Root referendum ends: its preimage is noted by a
signed account, and a scheduler agenda entry with the Root origin dispatches it. The proposals
are the ones build.py produces; only the sale period is shorter. Alice stands in for the
market and for whoever notes preimages.

Scenarios:
  main             someone creates the pool first; the whole lifecycle, the failure cases and
                   the unwind
  no-precreate     the same, with the pool created by the launch referendum
  liquidity-first  someone seeds the pool at another price first; the launch must fail whole

Exits with status 1 if a check fails.
"""

import argparse
import sys
from dataclasses import replace

from substrateinterface import Keypair

from jamkb_trickle.calls import Calls
from jamkb_trickle.chain import (
    agenda_len,
    asset_details,
    connect,
    dot_free,
    issuance,
    jamkb_balance,
    lp_balance,
    lp_supply,
    lp_token,
    module_error,
    outcome,
    pool_balances,
    price,
    relay_number,
    spec_version,
    succeeded,
    task_lookup,
)
from jamkb_trickle.config import fmt_dot, fmt_jamkb, load
from jamkb_trickle.encoding import (
    DOT_DECIMALS,
    JAMKB_DECIMALS,
    blake2_256,
    dot_location,
    jamkb_location,
    task_id,
)

DOT, KB = 10**DOT_DECIMALS, 10**JAMKB_DECIMALS
failures = []


def check(ok, what):
    print(f"    {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        failures.append(what)


def transferred(evs, cfg, src, dst):
    """JAMKB moved from `src` to `dst` in the given events."""
    return sum(a["amount"] for m, n, a in evs if (m, n) == ("Assets", "Transferred")
               and a["asset_id"] == cfg.asset_id and a["from"] == src and a["to"] == dst)


def dot_transferred(evs, src, dst):
    return sum(a["amount"] for m, n, a in evs if (m, n) == ("Balances", "Transfer")
               and a["from"] == src and a["to"] == dst)


def with_task(cfg, name, **sale):
    return replace(cfg, sale=replace(cfg.sale, task_name=name, task_id=task_id(name), **sale))


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

    def fund_alice(self, dot=1_000_000):
        self.rpc("dev_setStorage", [{"System": {"Account": [[[self.alice.ss58_address],
                 {"providers": 1, "data": {"free": str(dot * DOT)}}]]}}])

    def signed(self, call):
        """Alice sends `call`; returns the events of the block that includes it. Raises if
        the extrinsic failed."""
        ext = self.s.create_signed_extrinsic(call=call, keypair=self.alice)
        self.s.submit_extrinsic(ext, wait_for_inclusion=False)
        evs = self.new_block()
        for m, n, a in evs:
            if (m, n) == ("System", "ExtrinsicFailed"):
                raise RuntimeError(f"{call.call_module['name']}.{call.call_function['name']} "
                                   f"failed: {module_error(self.s, {'Err': a['dispatch_error']})}")
        return evs

    def fill_agenda(self, keys):
        """Fills the agendas of `keys` with 50 remarks each, as anyone could with referendum
        alarms."""
        remark = self.s.compose_call("System", "remark", {"remark": "0x"})
        entry = {"maybeId": None, "priority": 255, "maybePeriodic": None,
                 "origin": {"system": "Root"}, "call": {"Inline": remark.data.to_hex()}}
        self.rpc("dev_setStorage", [{"Scheduler": {"Agenda": [[[k], [entry] * 50] for k in keys]}}])

    def enact(self, label, call, expect="ok", prefill=None):
        """Notes the preimage and dispatches `call` with the Root origin, as an enacted
        referendum does. `expect` is "ok", or the error the referendum must fail with.
        `prefill` is a function of the chosen agenda block returning blocks to fill first.
        Returns (result, failed items, events of the enactment block)."""
        data = bytes(call.data.data)
        h = "0x" + blake2_256(data).hex()
        if not self.s.query("Preimage", "RequestStatusFor", [h], block_hash=self.head).value:
            self.signed(self.s.compose_call("Preimage", "note_preimage", {"bytes": "0x" + data.hex()}))
        # dev_setStorage replaces the whole agenda of a block: take an empty one that the next
        # blocks service (chopsticks moves the relay number by 4 per block).
        relay = relay_number(self.s, self.head)
        at = next(b for b in range(relay + 1, relay + 5)
                  if not self.s.query("Scheduler", "Agenda", [b], block_hash=self.head).value)
        if prefill:
            self.fill_agenda([k for k in prefill(at) if k != at])
        self.rpc("dev_setStorage", [{"Scheduler": {"Agenda": [[[at], [{
            "maybeId": None, "priority": 63, "maybePeriodic": None,
            "origin": {"system": "Root"}, "call": {"Lookup": {"hash": h, "len": len(data)}},
        }]]]}}])
        # The scheduler reads the relay number of the parent block in `on_initialize`, so the
        # entry runs in the first block whose parent's relay number has reached `at`.
        for _ in range(3):
            evs = self.new_block()
            result, items = outcome(self.s, evs, task=(at, 0))
            if result != "not dispatched":
                break
        ok = succeeded(result, items)
        shown = "Ok" if ok else (result if result != "Ok" else f"Ok with failed items {items}")
        print(f"\n[referendum] {label}: {shown} ({len(data)} bytes)")
        for i in items:
            print(f"    failed item: {i}")
        for m, n, a in evs:
            if (m, n) in {("Scheduler", "Scheduled"), ("Scheduler", "Canceled")}:
                print(f"    {m}.{n} {a}")
        if expect == "ok":
            check(ok, f"{label} enacted")
        else:
            allowed = expect if isinstance(expect, tuple) else (expect,)
            check(result in allowed, f"{label} fails with {' or '.join(allowed)} (got {result})")
        return result, items, evs

    def next_run(self, cfg, max_blocks=12):
        """Builds blocks until the task runs. Returns a dict with the result, the DOT burned
        from the sink, the JAMKB sold, and whether the issuance moved by exactly the burns and
        mints of the block."""
        task = "0x" + cfg.sale.task_id.hex()
        for _ in range(max_blocks):
            k0, i0 = self.q(jamkb_balance, cfg, cfg.seller), self.q(issuance)
            evs = self.new_block()
            runs = [(n, a) for m, n, a in evs
                    if m == "Scheduler" and a.get("id") == task and n != "Scheduled"]
            if not runs:
                continue
            n0, a0 = runs[0]
            result = module_error(self.s, a0["result"]) if n0 == "Dispatched" else n0
            burned = sum(a["amount"] for m, n, a in evs
                         if (m, n) == ("Balances", "Burned") and a["who"] == cfg.sink)
            minted = sum(a["amount"] for m, n, a in evs if (m, n) in {
                ("Balances", "Minted"), ("Balances", "Issued")})
            removed = sum(a["amount"] for m, n, a in evs if (m, n) in {
                ("Balances", "Burned"), ("Balances", "Rescinded")})
            i1 = self.q(issuance)
            d, k = self.q(pool_balances, cfg)
            print(f"\n[sale @relay {relay_number(self.s, self.head)}] {result}: burned "
                  f"{fmt_dot(burned)}, sold {fmt_jamkb(k0 - self.q(jamkb_balance, cfg, cfg.seller))}, "
                  f"pool price {price(d, k):.5f}")
            return {
                "result": result, "burned": burned,
                "sold": k0 - self.q(jamkb_balance, cfg, cfg.seller),
                "issuance_ok": i1 == i0 + minted - removed and i0 - i1 == burned - minted,
                "sink_killed": any((m, n) == ("System", "KilledAccount") and a["account"] == cfg.sink
                                   for m, n, a in evs),
                "events": evs,
            }
        return None


def create_and_check(f, C, cfg):
    f.enact("A. create JAMKB", C.create_jamkb())
    a = f.q(asset_details, cfg)
    check(f.q(jamkb_balance, cfg, cfg.custody) == cfg.cap and a["supply"] == cfg.cap,
          "the cap is in custody and is the whole supply")
    check({a["owner"], a["issuer"], a["admin"], a["freezer"]} == {cfg.custody},
          "custody holds all four asset roles")


def alice_creates_pool(f, cfg):
    evs = f.signed(f.s.compose_call("AssetConversion", "create_pool", {
        "asset1": dot_location(), "asset2": jamkb_location(cfg.asset_id)}))
    created = [a for m, n, a in evs if (m, n) == ("AssetConversion", "PoolCreated")]
    check(bool(created) and created[0]["pool_account"] == cfg.pool,
          "someone created the pool; the derived pool account is the chain's")


def scenario_liquidity_first(f, C, cfg):
    create_and_check(f, C, cfg)
    f.enact("holder grant to Alice", C.grant(f.alice.ss58_address, 10_000 * KB))
    alice_creates_pool(f, cfg)
    f.signed(f.s.compose_call("AssetConversion", "add_liquidity", {
        "asset1": dot_location(), "asset2": jamkb_location(cfg.asset_id),
        "amount1_desired": 1_000 * DOT, "amount2_desired": 5_000 * KB,
        "amount1_min": 1, "amount2_min": 1, "mint_to": f.alice.ss58_address}))
    d, k = f.q(pool_balances, cfg)
    print(f"\nAlice seeded the pool first at price {price(d, k):.5f}")
    treasury0 = f.q(dot_free, cfg.treasury)
    result, _, _ = f.enact("B. launch over a pool seeded at another price", C.launch(),
                           expect=("AssetConversion.AssetOneDepositDidNotMeetMinimum",
                                   "AssetConversion.AssetTwoDepositDidNotMeetMinimum"))
    check(f.q(jamkb_balance, cfg, cfg.seller) == 0 and f.q(dot_free, cfg.seller) == 0 and
          f.q(dot_free, cfg.treasury) == treasury0 and f.q(task_lookup, cfg) is None,
          "nothing moved: no DOT left the treasury, the seller holds nothing, no sale scheduled")


def scenario_main(f, C, cfg, precreate):
    s = f.s
    create_and_check(f, C, cfg)
    if precreate:
        alice_creates_pool(f, cfg)

    # ── B. launch ──────────────────────────────────────────────────────────────────────
    _, _, evs = f.enact("B. launch (seed + start)", C.launch())
    lp = f.q(lp_token, cfg)
    check(lp is not None, "the pool exists after the launch")
    d, k = f.q(pool_balances, cfg)
    check(k == cfg.seed_jamkb and d >= cfg.seed_dot, f"pool seeded: {fmt_dot(d)} / {fmt_jamkb(k)}")
    added = [a for m, n, a in evs if (m, n) == ("AssetConversion", "LiquidityAdded")]
    check(bool(added) and added[0]["mint_to"] == cfg.pool and
          f.q(lp_balance, lp, cfg.pool) == f.q(lp_supply, lp) and
          f.q(lp_balance, lp, cfg.seller) == 0,
          "the whole pool share is held by the pool account; the seller holds none")
    check(f.q(jamkb_balance, cfg, cfg.seller) == cfg.tranche_jamkb, "the seller holds the tranche")
    check(f.q(task_lookup, cfg) is not None, "the sale is scheduled")

    # ── The submission path, with the proposal of a top-up ─────────────────────────────
    from submit import note_call, preimage_len, submit_call
    top = C.topup()
    data = bytes(top.data.data)
    built = {"call_data": "0x" + data.hex(), "preimage_hash": "0x" + blake2_256(data).hex(),
             "preimage_len": len(data)}
    if preimage_len(s, built["preimage_hash"]) is None:
        f.signed(note_call(s, built))
    evs = f.signed(submit_call(s, cfg, built))
    sub = [a for m, n, a in evs if (m, n) == ("Referenda", "Submitted")]
    check(bool(sub) and sub[0]["proposal"] == {"Lookup": {"hash": built["preimage_hash"],
                                                          "len": built["preimage_len"]}}
          and sub[0]["track"] == 0, "submit.py's referendum is on the Root track with the "
                                    "proposal's hash and length")

    # ── Sales ──────────────────────────────────────────────────────────────────────────
    x, y = cfg.sale.dot_per_period, cfg.sale.max_jamkb_per_period
    for i in (1, 2):
        r = f.next_run(cfg)
        check(r["result"] == "Ok" and r["burned"] == x and 0 < r["sold"] <= y,
              f"period {i}: burned exactly X, sold at most Y")
        check(r["issuance_ok"], f"period {i}: total issuance fell by the burn (net of the "
                                "block's mints)")
        check(r["sink_killed"] and f.q(dot_free, cfg.sink) == 0, f"period {i}: the sink is reaped")

    # ── C. change under the same id ────────────────────────────────────────────────────
    cfg_c = replace(cfg, sale=replace(cfg.sale, dot_per_period=100 * DOT,
                                      max_jamkb_per_period=1250 * KB))
    f.enact("C. change X to 100 DOT", Calls(s, cfg_c).change())
    r = f.next_run(cfg_c)
    check(r["result"] == "Ok" and r["burned"] == 100 * DOT,
          "after the change each period burns the new X")

    # ── The market: below the floor, and back ──────────────────────────────────────────
    f.enact("holder grant to Alice (market stand-in)", C.grant(f.alice.ss58_address, 2_000_000 * KB))
    jam, dot = jamkb_location(cfg.asset_id), dot_location()
    f.signed(s.compose_call("AssetConversion", "swap_exact_tokens_for_tokens", {
        "path": [jam, dot], "amount_in": 1_500_000 * KB, "amount_out_min": 1,
        "send_to": f.alice.ss58_address, "keep_alive": True}))
    d, k = f.q(pool_balances, cfg)
    print(f"\nAlice dumped JAMKB: price {price(d, k):.5f}, floor {cfg_c.sale.floor}")
    r = f.next_run(cfg_c)
    check(r["result"] == "AssetConversion.ProvidedMaximumNotSufficientForSwap" and
          r["burned"] == 0 and r["sold"] == 0, "below the floor the run fails and nothing is burned")
    check(f.q(task_lookup, cfg) is not None, "the task stays scheduled after a failed run")
    f.signed(s.compose_call("AssetConversion", "swap_tokens_for_exact_tokens", {
        "path": [dot, jam], "amount_out": 1_550_000 * KB, "amount_in_max": 1_000_000 * DOT,
        "send_to": f.alice.ss58_address, "keep_alive": True}))
    d, k = f.q(pool_balances, cfg)
    print(f"\nAlice bought back: price {price(d, k):.5f}")
    r = f.next_run(cfg_c)
    check(r["result"] == "Ok" and r["burned"] == 100 * DOT, "above the floor the sale resumes")

    # ── DOT sent to the sink ───────────────────────────────────────────────────────────
    f.signed(s.compose_call("Balances", "transfer_keep_alive", {"dest": cfg.sink, "value": DOT}))
    r = f.next_run(cfg_c)
    check(r["result"] == "Ok" and r["burned"] == 100 * DOT and f.q(dot_free, cfg.sink) == DOT,
          "DOT sent to the sink: the burn is still X, the extra stays in the sink")

    # ── D. top up, E. stop ─────────────────────────────────────────────────────────────
    _, _, evs = f.enact("D. top up", C.topup())
    check(transferred(evs, cfg, cfg.custody, cfg.seller) == cfg.topup_jamkb, "the seller got the top-up")
    _, _, evs = f.enact("E. stop", C.stop())
    back = transferred(evs, cfg, cfg.seller, cfg.custody)
    check(f.q(task_lookup, cfg) is None, "the task is cancelled")
    check(back > 0 and f.q(jamkb_balance, cfg, cfg.seller) == 0,
          f"the unsold {fmt_jamkb(back)} returned to custody")
    check(f.next_run(cfg, max_blocks=6) is None, "no sale after the stop")

    # ── F. count is the number of runs; G, H. stop once the task is gone ───────────────
    cfg_n = with_task(cfg, "jamkb-count-test", count=2)
    Cn = Calls(s, cfg_n)
    f.enact("F. top up and start a sale with count 2", Cn.batch_all([Cn.topup(), Cn.start()]))
    runs = 0
    while f.next_run(cfg_n) is not None:
        runs += 1
    check(runs == 2, f"count 2 runs the sale exactly 2 times (ran {runs})")
    check(f.q(task_lookup, cfg_n) is None, "the task is gone after its last run")
    held = f.q(jamkb_balance, cfg, cfg.seller)
    check(held > 0, "the seller still holds JAMKB before the stop tests")
    f.enact("G. stop in batch_all once the task is gone", Cn.stop_batch_all(),
            expect="Scheduler.NotFound")
    check(f.q(jamkb_balance, cfg, cfg.seller) == held, "batch_all failed whole: nothing returned")
    _, items, _ = f.enact("H. stop in force_batch once the task is gone", Cn.stop())
    check(items == ["Scheduler.NotFound"] and f.q(jamkb_balance, cfg, cfg.seller) == 0,
          "force_batch returns the tranche; only the cancel failed")

    # ── I. a change whose new schedule cannot be placed is reverted ────────────────────
    # The test sale's first run is far away, out of the agendas filled below; the change
    # would place the new schedule in one of them.
    cfg_ct = with_task(cfg, "jamkb-change-test", period=400, count=5, after=200)
    Cc = Calls(s, cfg_ct)
    f.enact("I1. start a sale with a long period", Cc.start())
    before = f.q(task_lookup, cfg_ct)
    f.enact("I2. change while every near agenda is full",
            Calls(s, replace(cfg_ct, sale=replace(cfg_ct.sale, after=1))).change(),
            expect="Exhausted", prefill=lambda at: range(at + 1, at + 12))
    check(f.q(task_lookup, cfg_ct) == before, "the cancel was reverted: the old sale is still scheduled")
    f.enact("I3. cancel the test sale", Cc.cancel_if_scheduled())

    # ── J. a full agenda drops the sale; the monitor alerts; change re-creates it ──────
    from monitor import Monitor

    class Collect(Monitor):
        def alert(self, number, text):
            self.alerts.append(text)

    cfg_d = with_task(cfg, "jamkb-drop-test", count=10)
    Cd = Calls(s, cfg_d)
    f.enact("J1. start a sale", Cd.start())
    when = f.q(task_lookup, cfg_d)[0]
    period = cfg_d.sale.period
    f.fill_agenda(range(when + period, when + period + 4))
    mon = Collect(cfg_d, None, 3)
    mon.alerts = []
    mon.scheduled = True
    dropped = None
    for _ in range(6):
        evs = f.new_block()
        mon.scan(s, f.head, 0)
        if any(m == "Scheduler" and n == "PeriodicFailed" and a.get("id") == "0x" + cfg_d.sale.task_id.hex()
               for m, n, a in evs):
            dropped = True
            break
    mon.scan(s, f.head, 0)
    check(bool(dropped) and f.q(task_lookup, cfg_d) is None,
          "a full agenda at the next slot drops the sale for good (PeriodicFailed)")
    check(any("PeriodicFailed" in t for t in mon.alerts) and
          any("not scheduled" in t for t in mon.alerts), "the monitor alerts on the drop")
    _, items, _ = f.enact("J2. change re-creates the dropped sale", Cd.change())
    check(items == ["Scheduler.NotFound"] and f.q(task_lookup, cfg_d) is not None,
          "change tolerates the missing task and schedules it again")
    f.enact("J3. cancel the test sale", Cd.cancel_if_scheduled())

    # ── K. unwind: the seed comes back ─────────────────────────────────────────────────
    lp_held = f.q(lp_balance, lp, cfg.pool)
    lp_burn = lp_held - lp_held // 10_000
    d0, k0 = f.q(pool_balances, cfg)
    treasury0 = f.q(dot_free, cfg.treasury)
    _, _, evs = f.enact("K. unwind", C.unwind(lp_burn))
    d1, k1 = f.q(pool_balances, cfg)
    to_custody = transferred(evs, cfg, cfg.seller, cfg.custody)
    to_treasury = f.q(dot_free, cfg.treasury) - treasury0
    print(f"    pool {fmt_dot(d0)} / {fmt_jamkb(k0)} -> {fmt_dot(d1)} / {fmt_jamkb(k1)}; "
          f"{fmt_jamkb(to_custody)} to custody, {fmt_dot(to_treasury)} to the treasury")
    check(f.q(lp_balance, lp, cfg.pool) == lp_held - lp_burn and d1 < d0 // 1000 and k1 < k0 // 1000,
          "the pool share is burned down to 1/10000; the pool keeps only a small rest")
    ed = s.get_constant("Balances", "ExistentialDeposit").value
    held = s.query("System", "Account", [cfg.seller], block_hash=f.head).value["data"]["reserved"]
    print(f"    seller left with {fmt_dot(f.q(dot_free, cfg.seller))} free, {fmt_dot(held)} held")
    # If the launch created the pool, the seller paid the deposits of the pool's two accounts;
    # they stay held on it, so it keeps the existential deposit too.
    check(to_custody >= k0 - k1 - 1 and to_treasury >= d0 - d1 and
          f.q(dot_free, cfg.seller) <= ed and f.q(jamkb_balance, cfg, cfg.seller) == 0,
          "the pool's JAMKB went to custody, its DOT and the seller's free DOT to the treasury")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/simulation.json")
    p.add_argument("--scenario", choices=["main", "no-precreate", "liquidity-first"], default="main")
    args = p.parse_args()
    cfg = load(args.config)
    f = Fork(cfg)
    C = Calls(f.s, cfg)
    spec = spec_version(f.s, f.head)
    print(f"fork of Asset Hub Polkadot, spec {spec}, scenario {args.scenario}; custody "
          f"{cfg.custody}, seller {cfg.seller}, sink {cfg.sink}, pool {cfg.pool}")
    if spec != cfg.tested_spec_version:
        print(f"note: runtime spec {spec} differs from the tested {cfg.tested_spec_version}")
    f.fund_alice()

    if args.scenario == "liquidity-first":
        scenario_liquidity_first(f, C, cfg)
    else:
        scenario_main(f, C, cfg, precreate=args.scenario == "main")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
    for w in failures:
        print(f"  FAILED: {w}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
