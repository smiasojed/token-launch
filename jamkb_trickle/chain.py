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
