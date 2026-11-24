"""Builds the referendum proposals. Every proposal is dispatched with the Root origin.

Each builder returns a `GenericCall` composed against the live metadata of the connected chain.
"""

from substrateinterface import SubstrateInterface

from .config import Config
from .encoding import dot_location, jamkb_location


def _signed(who: str) -> dict:
    return {"system": {"Signed": who}}


class Calls:
    def __init__(self, substrate: SubstrateInterface, cfg: Config):
        self.s = substrate
        self.c = cfg

    def call(self, _pallet: str, _function: str, **params):
        return self.s.compose_call(_pallet, _function, params)

    def as_fallible(self, who: str, call):
        """Dispatch `call` as `who`; an error fails the enclosing `batch_all`."""
        return self.call("Utility", "dispatch_as_fallible", as_origin=_signed(who), call=call)

    def as_infallible(self, who: str, call):
        """Dispatch `call` as `who`; an error is only reported in `DispatchedAs`."""
        return self.call("Utility", "dispatch_as", as_origin=_signed(who), call=call)

    def batch_all(self, calls):
        return self.call("Utility", "batch_all", calls=calls)

    def force_batch(self, calls):
        return self.call("Utility", "force_batch", calls=calls)

    # ── Referendum A: create JAMKB (only while pallet-jamkb is not deployed) ────────────

    def create_jamkb(self):
        c = self.c
        return self.batch_all([
            # Custody needs a provider (DOT) before it can hold the non-sufficient asset.
            self.call("Balances", "force_transfer", source=c.treasury, dest=c.custody,
                      value=c.custody_dot),
            self.call("Assets", "force_create", id=c.asset_id, owner=c.custody,
                      is_sufficient=False, min_balance=1),
            self.call("Assets", "force_set_metadata", id=c.asset_id, name=c.asset_name,
                      symbol=c.asset_symbol, decimals=3, is_frozen=False),
            self.as_fallible(c.custody, self.call("Assets", "mint", id=c.asset_id,
                                                  beneficiary=c.custody, amount=c.cap)),
        ])

    def grant(self, who: str, amount: int):
        """Moves undistributed JAMKB from custody to `who`."""
        if self.c.use_pallet_jamkb:
            return self.call("Jamkb", "grant", beneficiary=who, amount=amount)
        return self.as_fallible(self.c.custody, self.call(
            "Assets", "transfer", id=self.c.asset_id, target=who, amount=amount))

    # ── Referendum B: seed the pool and start the sale ──────────────────────────────────

    def seed_calls(self):
        c = self.c
        dot, jamkb = dot_location(), jamkb_location(c.asset_id)
        return [
            # Seed DOT, pool setup fee and existential deposit. Before the grant: the seller
            # needs a provider to hold JAMKB.
            self.call("Balances", "force_transfer", source=c.treasury, dest=c.seller,
                      value=c.seed_dot + c.seller_dot_extra),
            self.grant(c.seller, c.seed_jamkb + c.tranche_jamkb),
            # Infallible: a pool that someone created first does not fail the referendum.
            self.as_infallible(c.seller, self.call("AssetConversion", "create_pool",
                                                   asset1=dot, asset2=jamkb)),
            # The pool share goes to the pool account, where only Root can reach it.
            self.as_fallible(c.seller, self.call(
                "AssetConversion", "add_liquidity", asset1=dot, asset2=jamkb,
                amount1_desired=c.seed_dot, amount2_desired=c.seed_jamkb,
                amount1_min=c.seed_min_dot, amount2_min=c.seed_min_jamkb, mint_to=c.pool)),
        ]

    def sale_call(self):
        """The call the scheduler runs every period."""
        c, s = self.c, self.c.sale
        return self.batch_all([
            self.as_fallible(c.seller, self.call(
                "AssetConversion", "swap_tokens_for_exact_tokens",
                path=[jamkb_location(c.asset_id), dot_location()],
                amount_out=s.dot_per_period, amount_in_max=s.max_jamkb_per_period,
                send_to=c.sink, keep_alive=False)),
            self.as_fallible(c.sink, self.call("Balances", "burn", value=s.dot_per_period,
                                               keep_alive=False)),
        ])

    def schedule_sale(self):
        s = self.c.sale
        return self.call("Scheduler", "schedule_named_after", id="0x" + s.task_id.hex(),
                         after=s.after, maybe_periodic=(s.period, s.count),
                         priority=s.priority, call=self.sale_call())

    def seed(self):
        return self.batch_all(self.seed_calls())

    def start(self):
        return self.schedule_sale()

    def launch(self):
        """Seed and start in one referendum: the sale never runs without its pool."""
        return self.batch_all(self.seed_calls() + [self.schedule_sale()])

    # ── Later referenda ─────────────────────────────────────────────────────────────────

    def change(self):
        """Replaces the schedule with the parameters in the config, under the same task id.

        `force_batch`: `cancel_named` fails when the task is already gone, and the new
        schedule must still be set.
        """
        return self.force_batch([
            self.call("Scheduler", "cancel_named", id="0x" + self.c.sale.task_id.hex()),
            self.schedule_sale(),
        ])

    def topup(self):
        return self.grant(self.c.seller, self.c.topup_jamkb)

    def stop(self):
        """Stops the sale and returns the unsold tranche to custody.

        `force_batch`: in a `batch_all`, a `cancel_named` of a task that is gone (its count
        ran out, or the scheduler dropped it) fails the whole referendum.
        """
        c = self.c
        return self.force_batch([
            self.call("Scheduler", "cancel_named", id="0x" + c.sale.task_id.hex()),
            self.as_fallible(c.seller, self.call("Assets", "transfer_all", id=c.asset_id,
                                                 dest=c.custody, keep_alive=False)),
        ])

    def stop_batch_all(self):
        """The unsafe form of `stop`, kept for the simulation's negative test."""
        c = self.c
        return self.batch_all([
            self.call("Scheduler", "cancel_named", id="0x" + c.sale.task_id.hex()),
            self.as_fallible(c.seller, self.call("Assets", "transfer_all", id=c.asset_id,
                                                 dest=c.custody, keep_alive=False)),
        ])

    def by_step(self, step: str):
        return {
            "create": self.create_jamkb,
            "seed": self.seed,
            "start": self.start,
            "launch": self.launch,
            "change": self.change,
            "topup": self.topup,
            "stop": self.stop,
        }[step]()


STEPS = ["create", "seed", "start", "launch", "change", "topup", "stop"]
