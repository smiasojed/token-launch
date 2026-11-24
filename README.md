# JAMKB scheduled trickle on Asset Hub Polkadot

This repository sets up the sale of the DAO's JAMKB for DOT on Asset Hub Polkadot, and
burns the DOT the sale receives. It uses existing extrinsics only: no contract, no new pallet,
and no `pallet-jamkb`. Every DAO action is a Root referendum.

It contains:

- the flow, referendum by referendum, with every extrinsic explained;
- Python scripts that build the referendum proposals, check them against the chain, rehearse
  them on a fork, submit them, and watch the sale;
- a simulation that runs every referendum on a local fork of Asset Hub Polkadot and checks
  the result, including the failure cases.

Tested on Asset Hub Polkadot runtime spec 2006000. If the runtime has changed, run the
simulation again before submitting anything (`build.py` warns about this).

## Contents

1. [Overview](#1-overview)
2. [Accounts](#2-accounts)
3. [Parameters](#3-parameters)
4. [The referenda, one by one](#4-the-referenda-one-by-one)
5. [Extrinsic reference](#5-extrinsic-reference)
6. [Running it on Polkadot](#6-running-it-on-polkadot)
7. [Risks](#7-risks)
8. [Monitoring and failure modes](#8-monitoring-and-failure-modes)
9. [Simulation](#9-simulation)
10. [Files](#10-files)
11. [Known limits](#11-known-limits)

## 1. Overview

JAMKB is a `pallet-assets` asset (3 decimals, 21,000,000 cap, non-sufficient). Before
`pallet-jamkb` exists, the DAO holds it in a keyless custody account. Nobody has its key.
Only Root can move it, by dispatching a call as that account with `utility.dispatch_as`.

The DAO seeds a JAMKB/DOT pool in `pallet-asset-conversion` and gives a tranche of JAMKB to a
keyless seller account. The scheduler then runs one call every period, every hour by default:

```text
every period (scheduler, Root origin)
  utility.batch_all
    1. as seller: swap at most Y JAMKB for exactly X DOT, the DOT sent to the sink
    2. as sink:   burn X DOT
```

- Each period burns exactly `X` DOT. It sells as much JAMKB as that costs, up to `Y`.
- `X / Y` is the floor price. Below it the swap fails, the batch fails, and nothing is sold
  or burned. The next period runs as scheduled.
- `balances.burn` lowers the total issuance. The sink holds the DOT only within the call, and
  is reaped by the burn.
- Buyers buy JAMKB from the pool with an ordinary swap, from any wallet or DEX front end.

```text
          Root referendum
                │ dispatch_as
                ▼
treasury ──DOT──► seller ◄──JAMKB── custody (cap, keyless)
                    │  seed: add_liquidity
                    ▼
              JAMKB/DOT pool ◄── buyers swap DOT for JAMKB
                    ▲
                    │  every period: swap ≤ Y JAMKB for X DOT
                 seller ──X DOT──► sink ──burn──► (issuance − X)
```

## 2. Accounts

All accounts except the treasury are derived offline, so a referendum can name them
before they exist. `python build.py accounts` prints them for a config.

| Account  | Derivation | Address (default config) |
|----------|------------|--------------------------|
| treasury | `PalletId(*b"py/trsry")` | `13UVJyLnbVp9RBZYFwFGyDvVd1y27Tt8tkntv6Q7JVPhFsTB` |
| custody  | `PalletId(account_seed)` | `13UVJyLnbVp84aQVa1MzzQEZ19NrLKTKKsvz8MueHA3rb3L6` |
| seller   | `PalletId(account_seed).into_sub_account_truncating(*b"sell")` | `13UVJyLnbVp84aQVa1uKYC6JfKLVVo7oQYMNzzi348u3j3yu` |
| sink     | `PalletId(account_seed).into_sub_account_truncating(*b"sink")` | `13UVJyLnbVp84aQVa1uKnV8pLK4UVkb5FNyM67gdFR4AFRag` |
| pool     | `blake2_256(("py/ascon", (DOT, JAMKB)).encode())` | `1ZHC95RcT9nMn3AQmqKGGHotaF74xD6LqZA8J2bVg9PcheE` |

A `PalletId` account is `"modl" ++ seed ++ sub`, zero-padded to 32 bytes. The sub-account is
the raw 4 bytes (`*b"sell"`, a byte array): a `&str` or a `Vec` would add a length prefix and
give another address. The default `account_seed` is `py/jkbtr`. It must be 8 bytes and must
not be the `PalletId` of any pallet; in particular it must differ from the one `pallet-jamkb`
will use. The config loader rejects a few known ones; the list is not complete.

Locations, as `pallet-asset-conversion` names the assets:

- DOT: `{ parents: 1, interior: Here }`
- JAMKB: `{ parents: 0, interior: X2(PalletInstance(50), GeneralIndex(asset_id)) }`

The pool account depends on the asset id. The simulation checks the derived address
against the `PoolCreated` event.

## 3. Parameters

Set in `config/polkadot.json`. Amounts are decimal strings in DOT or JAMKB. The seed and
sale values in the file are examples; the DAO decides them. The loader rejects a config that
cannot work: a zero amount, minimums above the seed, a `count` below 2, a priority above 255.

| Field | Default | Meaning and constraints |
|-------|---------|-------------------------|
| `genesis_hash` | Asset Hub Polkadot | The scripts refuse to build or submit against another chain. |
| `asset.id` | 21000000 | A free `pallet-assets` id below `NextAssetId` (50,000,551 at spec 2006000). Signed accounts can only create the id in `NextAssetId`, so nobody but Root can take an id below it before the referendum enacts. `build.py create` checks both. |
| `asset.cap_jamkb` | 21000000 | Minted once, into custody. |
| `custody_dot` | 1 | DOT for custody, at least the existential deposit (0.01 DOT). JAMKB is non-sufficient, so an account needs DOT before it can hold JAMKB. |
| `seed.dot`, `seed.jamkb` | 50000, 500000 | The pool seed. Their ratio is the opening price (0.1 DOT per JAMKB here). |
| `seed.min_dot`, `seed.min_jamkb` | = seed | `add_liquidity` fails below these. Equal to the seed, the launch fails as a whole if someone added liquidity at another price first. |
| `seller_dot_extra` | 2 | DOT for the seller above the seed. Creating the pool costs 0.80226 DOT: the pool setup fee (0.40194) and two account deposits of 0.20016 that `create_pool` charges the creator (the pool's JAMKB account and its LP token account). With the existential deposit, at least 0.81226 DOT. The seller keeps the rest (1.19774 DOT here). |
| `tranche_jamkb` | 200000 | JAMKB the seller may sell. It bounds the DAO's exposure between referenda. |
| `topup_jamkb` | 100000 | JAMKB the `topup` referendum adds. |
| `sale.task_name` | jamkb-trickle-1 | The scheduler task id is `blake2_256(task_name)`. `change`, `stop` and `unwind` use the same id. |
| `sale.dot_per_period` | 12.5 | `X`, the DOT burned per period: 300 DOT a day, 0.025% of the seed's DOT per sale. At least the existential deposit. At about 0.3% of the pool's DOT or more, sandwiching the sale pays off (section 7); each quiet period takes `X` out of the pool, so the share grows over time. |
| `sale.floor_dot_per_jamkb` | 0.08 | The floor. `Y = X / floor`, rounded down, so the effective floor is never below it. It applies to the execution price, fee included, not to the pool's spot price. |
| `sale.period_relay_blocks` | 600 | The period in relay blocks: the Asset Hub scheduler counts relay blocks. 600 is 1 hour. Hourly sales are smaller than 4-hourly ones for the same daily amount, so each moves the price less and leaves more room against sandwiching. |
| `sale.count` | 8760 | The total number of runs; after the last one the task is gone. 8760 hourly runs is about a year. The tranche, not the `count`, bounds what the sale can sell, so the `count` is long: a renewal referendum takes weeks (section 6.8). |
| `sale.priority` | 200 | Scheduler priority, 0 to 255; a higher number runs later within the same relay block's agenda. Referendum enactments use 63 and referenda alarms 128. |
| `sale.after_relay_blocks` | 1 | The first run is at the enactment's relay block + `after` + 1. |
| `referendum.enactment_after_blocks` | 14400 | The Root track's minimum enactment period; a smaller value is raised to it. |

## 4. The referenda, one by one

Each referendum is one Root proposal. `build.py <step>` builds it. The referenda are
enacted in this order:

```text
A. create  ──►  B. launch  ──►  sale runs every period
                                   │
              C. change / D. top up / E. stop / F. unwind, as needed
```

`launch` can also be split into `seed` and `start`, as two referenda.

A referendum whose proposal is a `force_batch` dispatches `Ok` even when an item inside it
failed. Check every enactment for `Utility.ItemFailed` and `Utility.DispatchedAs` errors, not
only for `Scheduler.Dispatched` `Ok`. The only failures that are expected are listed with each
step; `rehearse.py` checks this.

### A. create: create JAMKB

Skip this step once `pallet-jamkb` is deployed; its `initialize` creates the asset (see
section 11).

`utility.batch_all` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `balances.force_transfer(treasury, custody, custody_dot)` | Custody needs DOT to hold a non-sufficient asset. |
| 2 | `assets.force_create(asset_id, custody, false, 1)` | Creates JAMKB. Custody becomes owner, issuer, admin and freezer. Non-sufficient, `min_balance` 1 plank. |
| 3 | `assets.force_set_metadata(asset_id, "JAMKB", "JAMKB", 3, false)` | Name, symbol, 3 decimals. |
| 4 | `utility.dispatch_as_fallible(Signed(custody), assets.mint(asset_id, custody, cap))` | Mints the cap into custody. Custody is the issuer, so the mint is dispatched as custody. |

Expected failures: none. After enactment: `Assets.Issued` of the cap to custody; custody
holds 21,000,000 JAMKB and all four asset roles.

### B. launch: seed the pool and start the sale

One referendum, so that the sale never runs without its pool. `utility.batch_all` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `balances.force_transfer(treasury, seller, seed.dot + seller_dot_extra)` | The seed DOT, the pool creation costs and the existential deposit. It must come before the grant: the seller needs DOT to hold JAMKB. |
| 2 | `utility.dispatch_as_fallible(Signed(custody), assets.transfer(asset_id, seller, seed.jamkb + tranche))` | The grant. With `pallet-jamkb` this is `jamkb.grant(seller, amount)`. |
| 3 | `utility.dispatch_as(Signed(seller), asset_conversion.create_pool(DOT, JAMKB))` | Creates the pool. `dispatch_as` succeeds even when the call fails, so a pool that someone created first does not fail the referendum. |
| 4 | `utility.dispatch_as_fallible(Signed(seller), asset_conversion.add_liquidity(DOT, JAMKB, seed.dot, seed.jamkb, seed.min_dot, seed.min_jamkb, pool))` | Seeds the pool. The pool share goes to the pool account itself, where only Root can reach it. |
| 5 | `scheduler.schedule_named_after(task_id, after, Some((period, count)), priority, sale_call)` | Schedules the sale. |

`sale_call`, run by the scheduler with the Root origin every period:

```text
utility.batch_all([
  utility.dispatch_as_fallible(Signed(seller),
    asset_conversion.swap_tokens_for_exact_tokens(
      path = [JAMKB, DOT], amount_out = X, amount_in_max = Y,
      send_to = sink, keep_alive = false)),
  utility.dispatch_as_fallible(Signed(sink),
    balances.burn(value = X, keep_alive = false)),
])
```

- `keep_alive = false` on the swap lets the tranche sell out.
- `dispatch_as_fallible` makes a failed swap fail the batch, so nothing is burned when
  nothing is sold. `dispatch_as` would swallow the error and the burn would fail on its own.
- The sale call is about 164 bytes, over the 128-byte inline limit. The scheduler stores it as
  a preimage itself; nothing else is needed.
- No retry is set. A retry would sell twice in one period.

Expected failures: `AssetConversion.PoolExists` from item 3, only if someone created the pool
first. After enactment:

- `AssetConversion.LiquidityAdded` with `mint_to` the pool account; the pool account holds
  the whole pool share;
- `Scheduler.Scheduled`;
- the pool holds the seed;
- the seller holds the tranche and 1.19774 DOT;
- `Scheduler.Lookup(task_id)` gives the first run.

### C. change: new rate or floor

Edit `sale` in the config and keep `task_name`. `utility.batch_all` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `utility.force_batch([scheduler.cancel_named(task_id)])` | Removes the running schedule. Inside `force_batch`, a task that is already gone (its count ran out, or the scheduler dropped it) is only an `ItemFailed`, and the referendum goes on. |
| 2 | `scheduler.schedule_named_after(task_id, …)` | The new schedule, under the same id. If it cannot be placed (its relay block's agenda is full: `Exhausted`), the outer `batch_all` reverts the cancel and the old sale keeps running. |

Expected failures: `Scheduler.NotFound` from item 1, only if the task was gone. `change`
also restarts a sale that the scheduler dropped.

### D. top up: more JAMKB for the seller

`utility.dispatch_as_fallible(Signed(custody), assets.transfer(asset_id, seller, topup))`,
or `jamkb.grant` with `pallet-jamkb`. Expected failures: none.

### E. stop: stop the sale and return the tranche

`utility.force_batch` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `scheduler.cancel_named(task_id)` | Stops the sale. |
| 2 | `utility.dispatch_as_fallible(Signed(seller), assets.transfer_all(asset_id, custody, false))` | Returns all unsold JAMKB to custody. |

`force_batch`, not `batch_all`: in a `batch_all`, a `cancel_named` of a task that is gone
fails the whole referendum, and the tranche is not returned. The simulation shows both.
Expected failures: `Scheduler.NotFound` if the task was gone; `Assets.NoAccount` if the
seller held no JAMKB.

Pause and resume are `stop` and a new `start`. Nothing else pauses the sale.

### F. unwind: end the sale and take the seed back

Built with the pool's current share: `build.py unwind` reads how much of the pool share the
pool account holds and burns all of it but 1/10,000. `utility.batch_all` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `utility.force_batch([scheduler.cancel_named(task_id)])` | Stops the sale if it still runs. |
| 2 | `utility.dispatch_as_fallible(Signed(pool), asset_conversion.remove_liquidity(DOT, JAMKB, lp_burn, 1, 1, seller))` | Takes the liquidity out, to the seller. The pool must keep each reserve at or above its minimum balance, so 1/10,000 of the share stays (5 DOT of a 50,000 DOT pool). |
| 3 | `utility.dispatch_as_fallible(Signed(seller), assets.transfer_all(asset_id, custody, false))` | JAMKB back to custody. |
| 4 | `utility.dispatch_as_fallible(Signed(seller), balances.transfer_all(treasury, false))` | DOT back to the treasury; the seller is reaped. |

All or nothing, except the cancel. With less than about 100 DOT in the pool, the rest may
fall below the existential deposit and `remove_liquidity` fails; `build.py` warns.

If the launch created the pool, the seller paid the deposits of the pool's two accounts
(0.40032 DOT). They stay held on the seller for as long as those accounts exist, so the seller
is not reaped and keeps the existential deposit too.

## 5. Extrinsic reference

| Extrinsic | Origin it needs | Used for |
|-----------|-----------------|----------|
| `balances.force_transfer(source, dest, value)` | Root | DOT from the treasury to custody and seller. It bypasses the treasury's spend tracks; a treasury spend can replace it. |
| `balances.burn(value, keep_alive)` | Signed (the sink) | Burns `X` DOT and lowers total issuance. |
| `balances.transfer_all(dest, keep_alive)` | Signed (seller) | Returns the seller's DOT at `unwind`. |
| `assets.force_create(id, owner, is_sufficient, min_balance)` | Root | Creates JAMKB. |
| `assets.force_set_metadata(id, name, symbol, decimals, is_frozen)` | Root | Sets JAMKB metadata. |
| `assets.mint(id, beneficiary, amount)` | Signed (the issuer, custody) | Mints the cap. |
| `assets.transfer(id, target, amount)` | Signed (custody) | The grant and the top-up. |
| `assets.transfer_all(id, dest, keep_alive)` | Signed (seller) | Returns the tranche. |
| `asset_conversion.create_pool(asset1, asset2)` | Signed (seller); costs 0.80226 DOT | Creates the JAMKB/DOT pool. |
| `asset_conversion.add_liquidity(asset1, asset2, amount1_desired, amount2_desired, amount1_min, amount2_min, mint_to)` | Signed (seller) | Seeds the pool; the share is minted to the pool account. |
| `asset_conversion.remove_liquidity(asset1, asset2, lp_token_burn, amount1_min_receive, amount2_min_receive, withdraw_to)` | Signed (the pool account) | Takes the seed back at `unwind`. |
| `asset_conversion.swap_tokens_for_exact_tokens(path, amount_out, amount_in_max, send_to, keep_alive)` | Signed (seller) | The sale: exactly `X` DOT out for at most `Y` JAMKB in. |
| `scheduler.schedule_named_after(id, after, maybe_periodic, priority, call)` | Root | Runs the sale every `period`, `count` times in total, with the Root origin. |
| `scheduler.cancel_named(id)` | Root | Stops the sale. Fails with `NotFound` when the task is gone. |
| `utility.dispatch_as(as_origin, call)` | Root | Dispatches as a keyless account; reports an error in `DispatchedAs` but succeeds. |
| `utility.dispatch_as_fallible(as_origin, call)` | Root | Dispatches as a keyless account; an error fails the call. |
| `utility.batch_all(calls)` | any | All or nothing. |
| `utility.force_batch(calls)` | any | Runs every call; failures do not stop the rest, and it succeeds anyway. |
| `preimage.note_preimage(bytes)` | Signed (anyone) | Stores a proposal for its referendum. |
| `referenda.submit(proposal_origin, proposal, enactment_moment)` | Signed (anyone) | Opens the referendum. |
| `referenda.place_decision_deposit(index)` | Signed (anyone) | Lets the referendum start deciding. |

## 6. Running it on Polkadot

### 6.1 Install

Requirements:

- Python 3.10 or newer (tested with 3.12);
- Node.js 22 or newer, for the fork only.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 6.2 Configure

Copy `config/polkadot.json` and set the values the DAO agreed on. Then print the accounts
and the derived sale numbers:

```bash
.venv/bin/python build.py --config config/polkadot.json accounts
```

Check that the asset id is free and below `NextAssetId` (`assets.asset(id)` is empty,
`assets.nextAssetId` is above it), and that the `task_name` is not in use
(`scheduler.lookup(blake2_256(task_name))` is empty). `build.py` checks all three.

### 6.3 Build each proposal

```bash
.venv/bin/python build.py --config config/polkadot.json create
.venv/bin/python build.py --config config/polkadot.json launch
```

Each writes `out/<step>.json`:

- `call_data`: the proposal, SCALE-encoded;
- `preimage_hash`, `preimage_len`: what the referendum names;
- `spec_version`, `genesis_hash`: the runtime and chain it was built against;
- `params`: chain values the build read (the pool share for `unwind`);
- `call`: the decoded call tree, to review.

`build.py` reads the chain and prints warnings: a changed runtime, an asset id that is taken
or at or above `NextAssetId`, a treasury too small, a pool that holds liquidity already, a
floor at or above the price, a seller with less than `Y`, a sale at 0.3% of the pool or more.
`launch` warns that the asset does not exist until `create` is enacted. That is expected;
build `launch` again against a fork where `create` was rehearsed (next step). With
`--strict`, any warning makes it exit with status 2.

Review `call` in the JSON, or paste `call_data` into polkadot.js Apps (Developer →
Extrinsics → Decode) on Asset Hub Polkadot.

### 6.4 Rehearse the exact proposal on a fork

`rehearse.py` enacts the exact bytes of `out/<step>.json` on a fork of the live chain, as an
approved Root referendum would, and checks the state it leaves:

```bash
./fork.sh                                                        # fresh fork of the live chain
.venv/bin/python build.py    --config config/polkadot.json create
.venv/bin/python rehearse.py --config config/polkadot.json out/create.json
.venv/bin/python build.py    --config config/polkadot.json launch --rpc ws://127.0.0.1:8000
.venv/bin/python rehearse.py --config config/polkadot.json out/launch.json
```

It fails on a failed referendum, on a failed item other than the expected ones, and on a
wrong state. For `start`, `launch` and `change` it also runs the first sale and checks it
burned exactly `X`. Each step must end with `rehearsal passed`.

`launch` built against the fork has the same bytes as `launch` built against Polkadot, as
long as both run the same runtime. Only the warnings differ.

### 6.5 Verify before the deposit and before enactment

The proposal bytes name pallets and calls by index and encode their arguments with the
types of the runtime they were built against. A runtime upgrade during the weeks a referendum
takes can make them undecodable, or make them mean something else. Before placing the
decision deposit, and again before enactment:

```bash
.venv/bin/python build.py --config config/polkadot.json verify out/launch.json
```

It decodes the bytes against the current runtime and compares them with a fresh build. On a
mismatch, the referendum must not pass as it is; a new one is needed.

### 6.6 Submit

A Root referendum on Polkadot (track 0, `root`) needs:

| | |
|---|---|
| submission deposit | 10 DOT |
| decision deposit | 100,000 DOT |
| prepare period | 1200 blocks |
| decision period | 403200 blocks (28 days) |
| confirm period | 14400 blocks (1 day) |
| minimum enactment | 14400 blocks (1 day) |
| deciding at once | 1 (other Root referenda queue) |

These are read from the chain at spec 2006000; check `referenda.tracks` before submitting.

```bash
export SURI="<the proposer's mnemonic>"     # a hot key with a few DOT; never commit it
.venv/bin/python submit.py --config config/polkadot.json note   out/create.json   # dry run
.venv/bin/python submit.py --config config/polkadot.json --execute note   out/create.json
.venv/bin/python submit.py --config config/polkadot.json --execute submit out/create.json
.venv/bin/python submit.py --config config/polkadot.json deposit <referendum index>  # dry run
```

`submit.py`:

- recomputes the preimage hash and length from `call_data`;
- refuses another chain, and a proposal built against another runtime version;
- refuses `submit` until the preimage is noted with the right length;
- prints the extrinsic, its fee and the deposit it holds;
- with `--execute`, asks you to type the action name, then waits for finality and exits
  with status 1 if the extrinsic failed.

Place the 100,000 DOT decision deposit from a cold wallet or a multisig, not from a key in an
environment variable: run `deposit` without `--execute` and submit the call data it prints
from the wallet. The preimage and the referendum can also be submitted from polkadot.js Apps
or Subsquare with the `call_data`.

Submit `launch` only after `create` is enacted. The treasury balance and the pool must be
checked again close to enactment: `build.py launch` repeats the checks.

### 6.7 After each enactment

Check the enactment block: `Scheduler.Dispatched` with `Ok` for the referendum, and no
`Utility.ItemFailed` or `Utility.DispatchedAs` error except the expected ones of section 4.
Then:

- after `create`: `assets.asset(id)` has custody as owner, issuer, admin and freezer, and
  custody holds the cap;
- after `launch`:
  - `assetConversion.LiquidityAdded` with `mint_to` the pool account;
  - `scheduler.lookup(task_id)` is set;
  - the first run, at the enactment's relay block + `after` + 1, shows `Balances.Burned` of
    `X` from the sink.

### 6.8 Renewals and emergency stops take weeks

Every change is a Root referendum. A Root referendum waits for the prepare period, decides
for up to 28 days, confirms for a day and enacts at least a day later. Only one decides at a
time on the Root track; others queue. Even with overwhelming support early, a stop takes over
two days; in practice, weeks. Plan for it:

- the default `count` is about a year, so the sale does not end before a renewal can pass;
- the tranche, not the `count`, bounds what can be sold between referenda;
- if the DAO wants a faster stop, the whitelisted-caller track (a Root call whitelisted by
  the Fellowship, with a 100-block confirm and enactment period) is the option to consider.
  It is not covered here.

## 7. Risks

- **Filling the scheduler agenda drops the sale.** A relay block's agenda holds 50 tasks.
  When a run re-schedules itself into a full agenda, the scheduler drops the task for good
  (`PeriodicFailed`). The next slot is public in `scheduler.lookup` an hour ahead, and
  anyone can fill it with referendum alarms for refundable deposits. The same trick on the
  block of a referendum's enactment loses the referendum. `monitor.py` alerts when the next
  slot fills up and when the task disappears; `change` restarts it. Running two named tasks
  staggered by half a period would keep one drop from halting the sale; this repository runs
  one.
- **Sandwiching.** The sale runs at a known time with a known size. A sandwich sells JAMKB in
  the block before and buys it back after the sale. It pays the pool fee twice, so it only
  profits when one sale moves the price by more than that: from about 0.3% of the pool's DOT
  per sale. The default, hourly sales of 0.025%, leaves a wide margin; the margin shrinks as
  the pool drains. This threshold is an estimate; the simulation does not run a sandwich.
- **The launch is public.** The enactment block of `launch` is known at least a day ahead, so
  the first buyers and the first sale can be front-run.
- **The floor.** A holder of a few percent of the pool's JAMKB can push the price under the
  floor before every sale, and the sale never runs. Setting a lower floor trades this against
  selling cheap.
- **Runtime upgrades.** The stored sale call is decoded when it runs. An upgrade that changes a
  call index or the `Location` encoding makes it undecodable (`CallUnavailable`) and drops it.
  A pending proposal can break the same way (section 6.5).
- **Treasury spending.** `balances.force_transfer` from the treasury bypasses its spend
  tracks and appears in no treasury spend record.

## 8. Monitoring and failure modes

```bash
.venv/bin/python monitor.py --config config/polkadot.json [--webhook URL]
```

It follows finalized blocks and prints every run of the task with its result, the DOT burned,
the pool's spot price and the JAMKB left with the seller. It reconnects after a dropped
connection. It prints an `ALERT` line, and posts it to `--webhook` as JSON `{"text": ...}`,
when:

- the task is not scheduled: at start-up, or when it disappears (cancelled, last run done,
  or dropped by the scheduler);
- the scheduler fails it for good: `PeriodicFailed`, `CallUnavailable` or
  `PermanentlyOverweight`;
- a run is overdue: the relay chain passed its slot by 10 blocks and it did not run;
- three runs in a row failed (`--failures`);
- the agenda of its next slot holds 40 or more of 50 entries.

| What happens | Effect | What to do |
|--------------|--------|------------|
| Price below the floor | The run fails with `AssetConversion.ProvidedMaximumNotSufficientForSwap`. Nothing is sold or burned. The task stays scheduled. | Nothing, or a `change` with a lower floor. |
| The tranche is sold out | The swap fails every period. | `topup`, or `stop`. |
| The next slot holds 50 scheduled tasks | The scheduler drops the task for good (`PeriodicFailed`). | `change` (it re-creates the task). |
| A runtime upgrade changes a call index or the `Location` encoding | `CallUnavailable`, the task is dropped. | Rebuild with the new metadata; `change`. |
| `count` runs out | The task is gone. | `change` before it runs out. |
| No Asset Hub block lands on the exact relay block, or higher-priority work fills the block | The sale runs later. The next run is one period after the actual run, so the cadence drifts. Agendas that are due run before later ones. | Nothing. |
| Someone sends DOT to the sink | The burn still burns `X`; the extra stays in the sink. | Nothing. |
| Someone adds liquidity before `launch` at another price | `add_liquidity` fails below the seed minimums, and the launch fails as a whole: nothing leaves the treasury. | Resubmit with matching amounts, or a pool that is not seeded. |

## 9. Simulation

The simulation runs every referendum on a local fork of Asset Hub Polkadot. It uses the
live runtime and live state; nothing is sent to Polkadot. The proposals are the ones
`build.py` produces. Only the period is shorter, 8 relay blocks instead of 600, so that a
sale runs every second block.

It enacts each proposal as an approved Root referendum ends: the preimage is noted by a
signed account, then an anonymous scheduler entry with the Root origin dispatches it. That is
faithful for the Root dispatch itself. A real enactment differs in that it is a named task
created by `referenda` with a requested preimage, after the vote and the deposits; the
simulation checks that `submit.py` opens the referendum correctly, but not the vote.

### 9.1 Setup

```bash
# Python
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Node.js 22 or newer for chopsticks (for example: nvm install 22 && nvm use 22).
# fork.sh installs chopsticks 1.5.2 into chopsticks/node_modules on its first run.
```

`chopsticks/ahp.yml` forks Asset Hub Polkadot at its latest block. Blocks are built only
on request (`build-block-mode: Manual`), so the simulation controls every block.
Chopsticks before 1.5 cannot build blocks for the current runtime (an Aura slot check
fails), and it needs Node.js 22.

### 9.2 Run

Each scenario needs a fresh fork:

```bash
./fork.sh && .venv/bin/python simulate.py --config config/simulation.json
./fork.sh && .venv/bin/python simulate.py --config config/simulation.json --scenario no-precreate
./fork.sh && .venv/bin/python simulate.py --config config/simulation.json --scenario liquidity-first
pkill -f chopsticks                                        # stop the fork
```

The main scenario takes about ten minutes. Each run prints every referendum, every sale and
a `PASS` or `FAIL` per check, and exits with status 1 if a check fails. `docs/` holds a
passing run of each scenario.

### 9.3 What it checks

| Step | Checked |
|------|---------|
| A. create | The cap is the whole supply and is in custody; custody holds all four roles. |
| Someone creates the pool first (main) | The derived pool account equals the one in `PoolCreated`. |
| B. launch | It succeeds with or without an existing pool. The pool holds the seed. The pool account holds the whole pool share (balance equals supply), the seller none. The seller holds the tranche. The task is scheduled. |
| `submit.py` | `note` and `submit` open a Root-track referendum with the proposal's hash and length. |
| Periods 1 and 2 | Each burns exactly `X` and sells at most `Y`. Total issuance falls by the burn, net of the block's mints. The sink is reaped. |
| C. change (X to 100 DOT, same id) | The next period burns the new `X`. |
| A holder dumps JAMKB under the floor | The run fails with `ProvidedMaximumNotSufficientForSwap`. Nothing is burned. The task stays scheduled. |
| The holder buys back | The next period sells again. |
| DOT sent to the sink | The burn is still `X`; the extra stays in the sink. |
| D. top up | The seller receives the top-up. |
| E. stop | The task is cancelled. The unsold tranche returns to custody. No sale runs after. |
| F. a sale with `count` 2 | It runs exactly twice, then the task is gone. |
| G. stop in `batch_all` once the task is gone | Fails with `Scheduler.NotFound`; nothing is returned. |
| H. stop in `force_batch` once the task is gone | Succeeds; only the cancel failed; the tranche returns. |
| I. change while the target agendas are full | Fails with `Exhausted`; the cancel is reverted and the old sale stays scheduled. |
| J. the next slot's agenda is full | The scheduler drops the sale (`PeriodicFailed`); the monitor alerts; `change` schedules it again. |
| K. unwind | The pool share is burned down to 1/10,000; the pool's JAMKB goes to custody and its DOT to the treasury; the seller is emptied. |
| Scenario `liquidity-first` | Someone seeds the pool at another price first: the launch fails as a whole, nothing leaves the treasury, no sale is scheduled. |

Alice (`//Alice`) stands in for the market: she notes the preimages and trades in the pool.
She is funded by writing storage on the fork. The other direct storage writes are the Root
scheduler entries that stand in for passed referenda, and the filled agendas of I and J.

### 9.4 What it does not check

- The real 600-block period and the year-long `count`.
- The vote itself and the decision deposit; the enactment is emulated as above.
- `pallet-jamkb`, which is not on Asset Hub Polkadot.

## 10. Files

| File | Purpose |
|------|---------|
| `config/polkadot.json` | Parameters for Asset Hub Polkadot. |
| `config/simulation.json` | The same, against the fork, with an 8-block period. |
| `jamkb_trickle/encoding.py` | Account, pool and task id derivations; locations. |
| `jamkb_trickle/config.py` | Loads and validates a config; derives accounts and amounts. |
| `jamkb_trickle/calls.py` | Builds every proposal. |
| `jamkb_trickle/chain.py` | Read helpers, and the enactment outcome check. |
| `build.py` | Builds a proposal, checks it against the chain, writes `out/<step>.json`; `verify` checks a built one. Read-only. |
| `rehearse.py` | Enacts a built proposal on the fork and checks the state. |
| `submit.py` | Notes the preimage, submits the referendum, places the decision deposit. Dry run by default. |
| `monitor.py` | Watches the sale and alerts when it stops or is at risk. |
| `simulate.py` | The full simulation, with checks. |
| `fork.sh`, `chopsticks/` | Starts the chopsticks fork. |
| `docs/` | Passing simulation runs. |

## 11. Known limits

- **Without `pallet-jamkb`, the cap is policy, not code.** Custody holds the issuer role, so a
  Root referendum could mint more JAMKB. There is no pause flag and no `BudgetGranted`
  record. There is no redemption to JAM either: that needs `pallet-jamkb`.
- **Moving to `pallet-jamkb` later.** Its `initialize` rejects an asset that already exists.
  The pallet would have to take over the asset and its roles (for example
  `assets.transfer_ownership` and `assets.set_team` dispatched as custody) and the custody
  balance. After that, set `"use_pallet_jamkb": true` in the config: grants become
  `jamkb.grant`. Neither path is tested.
- **Root only.** Every change, including an emergency stop, is a Root referendum (section 6.8).
- **No catching up.** A period that sells nothing is lost.
- **Proceeds.** Each period burns `X` DOT. In a quiet market that DOT is the seed coming back,
  not buyers' DOT. Buyers' DOT stays in the pool, and the pool fee accrues to the locked
  pool share until `unwind`.
