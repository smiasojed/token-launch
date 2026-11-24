#!/usr/bin/env python3
"""Notes a proposal's preimage and submits it as a Root referendum. Dry run by default.

    python build.py  --config config/polkadot.json launch          # writes out/launch.json
    python build.py  --config config/polkadot.json verify out/launch.json
    python submit.py --config config/polkadot.json note    out/launch.json
    python submit.py --config config/polkadot.json submit  out/launch.json
    python submit.py --config config/polkadot.json deposit <referendum index>

Without --execute it checks everything, prints the extrinsic, its fee and the deposits it
holds, and sends nothing. With --execute it asks you to type the action name to confirm, signs,
waits for finality and exits with status 1 if the extrinsic failed.

The signer is read from the SURI environment variable. Use a hot key only for `note` and
`submit` (deposits of a few DOT). Place the decision deposit (100,000 DOT on the Root track)
from a cold wallet or a multisig, with the call this script prints.
"""

import argparse
import json
import os
import sys

from substrateinterface import Keypair

from jamkb_trickle.chain import connect, spec_version
from jamkb_trickle.config import fmt_dot, load
from jamkb_trickle.encoding import blake2_256


def load_proposal(path):
    """Reads out/<step>.json and recomputes its hash and length from the call data."""
    with open(path) as f:
        built = json.load(f)
    data = bytes.fromhex(built["call_data"][2:])
    h = "0x" + blake2_256(data).hex()
    if h != built["preimage_hash"] or len(data) != built["preimage_len"]:
        sys.exit(f"ERROR: {path}: preimage_hash or preimage_len does not match call_data")
    return built


def check_chain(s, cfg, built=None):
    genesis = s.get_block_hash(0)
    if genesis != cfg.genesis_hash:
        sys.exit(f"ERROR: connected to genesis {genesis}, the config is for {cfg.genesis_hash}")
    spec = spec_version(s)
    if built and built.get("spec_version") != spec:
        sys.exit(f"ERROR: the proposal was built against spec {built.get('spec_version')}, the "
                 f"chain runs {spec}: run `build.py verify`, rebuild if needed")


def preimage_len(s, h):
    """The length of a noted preimage, or None if it is not noted."""
    v = s.query("Preimage", "RequestStatusFor", [h]).value
    if not v:
        return None
    status = next(iter(v.values()))
    return status.get("len") if "len" in status else status.get("maybe_len")


def note_call(s, built):
    return s.compose_call("Preimage", "note_preimage", {"bytes": built["call_data"]})


def submit_call(s, cfg, built):
    after = cfg.raw["referendum"]["enactment_after_blocks"]
    return s.compose_call("Referenda", "submit", {
        "proposal_origin": {"system": cfg.raw["referendum"]["track_origin"]},
        "proposal": {"Lookup": {"hash": built["preimage_hash"], "len": built["preimage_len"]}},
        "enactment_moment": {"After": after},
    })


def deposit_call(s, index):
    return s.compose_call("Referenda", "place_decision_deposit", {"index": int(index)})


def decision_deposit(s, index):
    info = s.query("Referenda", "ReferendumInfoFor", [int(index)]).value
    if not info or "Ongoing" not in info:
        sys.exit(f"ERROR: referendum {index} is not ongoing")
    track = info["Ongoing"]["track"]
    if info["Ongoing"].get("decision_deposit"):
        sys.exit(f"ERROR: referendum {index} has its decision deposit already")
    for tid, t in s.get_constant("Referenda", "Tracks").value:
        if tid == track:
            return t["decision_deposit"], track
    sys.exit(f"ERROR: unknown track {track}")


def send(s, call, action, holds, execute, yes, wait_finality=True):
    suri = os.environ.get("SURI")
    if not suri:
        sys.exit("set SURI to the signer's secret URI")
    kp = Keypair.create_from_uri(suri, ss58_format=0)
    info = s.get_payment_info(call, kp)
    print(f"signer   {kp.ss58_address}")
    print(f"call     {call.call_module['name']}.{call.call_function['name']}  "
          f"{len(bytes(call.data.data))} bytes, call data {call.data.to_hex()}")
    print(f"fee      ~{fmt_dot(info['partialFee'])}")
    print(f"holds    {holds}")
    if not execute:
        print("dry run: add --execute to send")
        return
    if not yes and input(f"type '{action}' to sign and send: ").strip() != action:
        sys.exit("not confirmed; nothing sent")
    ext = s.create_signed_extrinsic(call=call, keypair=kp)
    receipt = s.submit_extrinsic(ext, wait_for_inclusion=True, wait_for_finalization=wait_finality)
    print(f"included in {receipt.block_hash}")
    for e in receipt.triggered_events:
        ev = e.value["event"]
        if ev["module_id"] in ("Referenda", "Preimage"):
            print(f"  {ev['module_id']}.{ev['event_id']} {ev['attributes']}")
    if not receipt.is_success:
        print(f"FAILED: {receipt.error_message}")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("--execute", action="store_true", help="sign and send")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    p.add_argument("--no-finality", action="store_true",
                   help="wait for inclusion only (a fork that does not finalize)")
    p.add_argument("action", choices=["note", "submit", "deposit"])
    p.add_argument("target", help="out/<step>.json, or the referendum index for `deposit`")
    args = p.parse_args()

    cfg = load(args.config)
    s = connect(cfg.rpc)
    run = dict(execute=args.execute, yes=args.yes, wait_finality=not args.no_finality)

    if args.action == "deposit":
        check_chain(s, cfg)
        amount, track = decision_deposit(s, args.target)
        print(f"referendum {args.target} on track {track}")
        send(s, deposit_call(s, args.target), "deposit",
             f"{fmt_dot(amount)} decision deposit, refunded when the referendum ends", **run)
        return

    built = load_proposal(args.target)
    check_chain(s, cfg, built)
    noted = preimage_len(s, built["preimage_hash"])
    if args.action == "note":
        if noted is not None:
            print(f"preimage {built['preimage_hash']} is noted already ({noted} bytes)")
            return
        base = s.get_constant("Preimage", "BaseDeposit").value if _has(s, "Preimage", "BaseDeposit") else None
        send(s, note_call(s, built), "note",
             "a preimage deposit for " + f"{built['preimage_len']} bytes" +
             (f" (base {fmt_dot(base)})" if base else "") + ", refunded on unnote", **run)
    else:
        if noted != built["preimage_len"]:
            sys.exit(f"ERROR: the preimage {built['preimage_hash']} is not noted with length "
                     f"{built['preimage_len']} (found {noted}): run `note` first")
        dep = s.get_constant("Referenda", "SubmissionDeposit").value
        print(f"proposal {built['step']}: {built['preimage_hash']}, {built['preimage_len']} bytes, "
              f"origin {cfg.raw['referendum']['track_origin']}, enactment after "
              f"{cfg.raw['referendum']['enactment_after_blocks']} blocks")
        send(s, submit_call(s, cfg, built), "submit",
             f"{fmt_dot(dep)} submission deposit", **run)


def _has(s, pallet, const):
    return any(c.name == const for c in s.metadata.get_metadata_pallet(pallet).constants)


if __name__ == "__main__":
    main()
