"""Read helpers shared by build, monitor and simulate."""

from substrateinterface import SubstrateInterface

from .config import Config


def connect(url: str) -> SubstrateInterface:
    return SubstrateInterface(url=url, ss58_format=0)


def spec_version(s: SubstrateInterface, block_hash=None) -> int:
    return s.get_block_runtime_version(block_hash or s.get_chain_head())["specVersion"]


def dot_free(s, who: str, block_hash=None) -> int:
    return s.query("System", "Account", [who], block_hash=block_hash).value["data"]["free"]


def jamkb_balance(s, cfg: Config, who: str, block_hash=None) -> int:
    v = s.query("Assets", "Account", [cfg.asset_id, who], block_hash=block_hash).value
    return v["balance"] if v else 0


def asset_details(s, cfg: Config, block_hash=None):
    return s.query("Assets", "Asset", [cfg.asset_id], block_hash=block_hash).value


def pool_balances(s, cfg: Config, block_hash=None):
    """DOT and JAMKB held by the pool account. The pool's reserves, up to the DOT
    existential deposit."""
    return dot_free(s, cfg.pool, block_hash), jamkb_balance(s, cfg, cfg.pool, block_hash)


def price(dot: int, jamkb: int) -> float:
    """DOT per JAMKB."""
    return (dot / 10**10) / (jamkb / 10**3) if jamkb else float("nan")


def task_lookup(s, cfg: Config, block_hash=None):
    """(relay block, agenda index) of the next run, or None."""
    return s.query("Scheduler", "Lookup", ["0x" + cfg.sale.task_id.hex()],
                   block_hash=block_hash).value


def relay_number(s, block_hash=None) -> int:
    return s.query("ParachainSystem", "LastRelayChainBlockNumber", block_hash=block_hash).value


def issuance(s, block_hash=None) -> int:
    return s.query("Balances", "TotalIssuance", block_hash=block_hash).value


def lp_token(s, cfg: Config, block_hash=None):
    """The pool's LP token id, or None if the pool does not exist."""
    from .encoding import dot_location, jamkb_location

    v = s.query("AssetConversion", "Pools", [(dot_location(), jamkb_location(cfg.asset_id))],
                block_hash=block_hash).value
    return v["lp_token"] if v else None


def lp_balance(s, lp, who: str, block_hash=None) -> int:
    v = s.query("PoolAssets", "Account", [lp, who], block_hash=block_hash).value
    return v["balance"] if v else 0


def lp_supply(s, lp, block_hash=None) -> int:
    v = s.query("PoolAssets", "Asset", [lp], block_hash=block_hash).value
    return v["supply"] if v else 0


def agenda_len(s, relay_block: int, block_hash=None) -> int:
    """Occupied entries in the scheduler agenda of `relay_block` (at most MaxScheduledPerBlock)."""
    v = s.query("Scheduler", "Agenda", [relay_block], block_hash=block_hash).value
    return sum(1 for e in (v or []) if e is not None)


# Failures that do not make a referendum fail: `cancel_named` of a task that is already gone,
# `create_pool` of a pool that exists, and `transfer_all` from a seller that holds nothing.
TOLERATED = {"Scheduler.NotFound", "AssetConversion.PoolExists", "Assets.NoAccount"}


def outcome(s, events, task=None):
    """Reads an enactment block. `events` is [(pallet, event, attributes)]; `task` is the
    (when, index) agenda address of the referendum's entry.

    Returns (result, failed_items): `result` is the dispatch result of the entry; failed_items
    lists the errors of the items inside it (`ItemFailed` of a `force_batch`, `DispatchedAs`
    errors), so that an `Ok` with a failed item is not mistaken for success.
    """
    result, items = "not dispatched", []
    for m, n, a in events:
        if m == "Scheduler" and n == "Dispatched" and (
                task is None and a.get("id") is None or
                task is not None and tuple(a.get("task") or ()) == tuple(task)):
            result = module_error(s, a["result"])
        elif m == "Scheduler" and n == "CallUnavailable" and (
                task is None or tuple(a.get("task") or ()) == tuple(task)):
            result = "CallUnavailable"
        elif (m, n) == ("Utility", "ItemFailed"):
            items.append(module_error(s, {"Err": a["error"]}))
        elif (m, n) == ("Utility", "DispatchedAs") and "Err" in a["result"]:
            items.append(module_error(s, a["result"]))
    return result, items


def succeeded(result, items, tolerated=TOLERATED) -> bool:
    return result == "Ok" and all(i in tolerated for i in items)


def module_error(s, result) -> str:
    """Names a `DispatchResult` error from an event attribute."""
    if not isinstance(result, dict) or "Err" not in result:
        return "Ok"
    err = result["Err"]
    if isinstance(err, dict) and "Module" in err:
        m = err["Module"]
        e = m["error"]
        e_idx = e[0] if isinstance(e, (list, tuple, bytes)) else int(str(e)[2:4], 16)
        found = s.metadata.get_module_error(m["index"], e_idx)
        pallet = next(p.name for p in s.metadata.pallets if p.value["index"] == m["index"])
        return f"{pallet}.{found.name}" if found else f"{pallet}.error#{e_idx}"
    return str(err)
