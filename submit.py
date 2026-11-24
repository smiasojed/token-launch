#!/usr/bin/env python3
"""Notes a proposal's preimage and submits it as a Root referendum. Dry run by default.

    python build.py  --config config/polkadot.json launch          # writes out/launch.json
    python submit.py --config config/polkadot.json note   out/launch.json
    python submit.py --config config/polkadot.json submit out/launch.json
    python submit.py --config config/polkadot.json deposit <referendum index>

Add --execute to sign and send. The signer comes from the SURI environment variable
(a mnemonic, or a dev URI such as //Alice on a fork). Without --execute the script prints the
extrinsic and its estimated fee, and sends nothing.
"""

import argparse
import json
import os

from substrateinterface import Keypair

from jamkb_trickle.chain import connect
from jamkb_trickle.config import load


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


def send(s, call, execute: bool):
    suri = os.environ.get("SURI")
    if not suri:
        raise SystemExit("set SURI to the signer's secret URI")
    kp = Keypair.create_from_uri(suri, ss58_format=0)
    info = s.get_payment_info(call, kp)
    print(f"signer {kp.ss58_address}")
    print(f"call   {call.call_module['name']}.{call.call_function['name']}  "
          f"{len(bytes(call.data.data))} bytes, fee ~{info['partialFee'] / 1e10:.4f} DOT")
    if not execute:
        print("dry run: add --execute to send")
        return None
    ext = s.create_signed_extrinsic(call=call, keypair=kp)
    receipt = s.submit_extrinsic(ext, wait_for_inclusion=True)
    print(f"included in {receipt.block_hash}, success {receipt.is_success}")
    if not receipt.is_success:
        print(receipt.error_message)
    for e in receipt.triggered_events:
        ev = e.value["event"]
        if ev["module_id"] in ("Referenda", "Preimage"):
            print(f"  {ev['module_id']}.{ev['event_id']} {ev['attributes']}")
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default="config/polkadot.json")
    p.add_argument("--execute", action="store_true")
    p.add_argument("action", choices=["note", "submit", "deposit"])
    p.add_argument("target", help="out/<step>.json, or the referendum index for `deposit`")
    args = p.parse_args()

    cfg = load(args.config)
    s = connect(cfg.rpc)
    if args.action == "deposit":
        send(s, deposit_call(s, args.target), args.execute)
        return
    with open(args.target) as f:
        built = json.load(f)
    if args.action == "note":
        status = s.query("Preimage", "RequestStatusFor", [built["preimage_hash"]]).value
        if status:
            print(f"preimage {built['preimage_hash']} is noted already: {status}")
            return
        send(s, note_call(s, built), args.execute)
    else:
        send(s, submit_call(s, cfg, built), args.execute)


if __name__ == "__main__":
    main()
