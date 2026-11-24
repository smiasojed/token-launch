# JAMKB scheduled trickle on Asset Hub Polkadot

This repository sets up the sale of the DAO's JAMKB for DOT on Asset Hub Polkadot, and
burns the DOT the sale receives. It uses existing extrinsics only: no contract, no new pallet,
and no `pallet-jamkb`. Every DAO action is a Root referendum.

It contains:

- the flow, referendum by referendum, with every extrinsic explained;
- Python scripts that build the referendum proposals, check them against the chain, submit
  them, and watch the sale;
- a simulation that runs every referendum on a local fork of Asset Hub Polkadot and checks
  the result.

Tested on Asset Hub Polkadot runtime spec 2006000. If the runtime has changed, run the
simulation again before submitting anything (`build.py` warns about this).

## Contents

1. [Overview](#1-overview)
2. [Accounts](#2-accounts)
3. [Parameters](#3-parameters)
4. [The referenda, one by one](#4-the-referenda-one-by-one)
5. [Extrinsic reference](#5-extrinsic-reference)
6. [Running it on Polkadot](#6-running-it-on-polkadot)
7. [Monitoring and failure modes](#7-monitoring-and-failure-modes)
8. [Simulation](#8-simulation)
9. [Files](#9-files)
10. [Known limits](#10-known-limits)

## 1. Overview

JAMKB is a `pallet-assets` asset (3 decimals, 21,000,000 cap, non-sufficient). Before
`pallet-jamkb` exists, the DAO holds it in a keyless custody account. Nobody has its key.
Only Root can move it, by dispatching a call as that account with `utility.dispatch_as`.

The DAO seeds a JAMKB/DOT pool in `pallet-asset-conversion` and gives a tranche of JAMKB to a
keyless seller account. The scheduler then runs one call every period, 4 hours by default:

```
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

```
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
| treasury | `PalletId("py/trsry")` | `13UVJyLnbVp9RBZYFwFGyDvVd1y27Tt8tkntv6Q7JVPhFsTB` |
| custody  | `PalletId(account_seed)` | `13UVJyLnbVp84aQVa1MzzQEZ19NrLKTKKsvz8MueHA3rb3L6` |
| seller   | `PalletId(account_seed)` sub-account `"sell"` | `13UVJyLnbVp84aQVa1uKYC6JfKLVVo7oQYMNzzi348u3j3yu` |
| sink     | `PalletId(account_seed)` sub-account `"sink"` | `13UVJyLnbVp84aQVa1uKnV8pLK4UVkb5FNyM67gdFR4AFRag` |
| pool     | `blake2_256(("py/ascon", (DOT, JAMKB)).encode())` | `1ZHC95RcT9nMn3AQmqKGGHotaF74xD6LqZA8J2bVg9PcheE` |

A `PalletId` account is `"modl" ++ seed ++ sub`, zero-padded to 32 bytes. The default
`account_seed` is `py/jkbtr`. It must be 8 bytes and must not be the `PalletId` of any
pallet; in particular it must differ from the one `pallet-jamkb` will use.

Locations, as `pallet-asset-conversion` names the assets:

- DOT: `{ parents: 1, interior: Here }`
- JAMKB: `{ parents: 0, interior: X2(PalletInstance(50), GeneralIndex(asset_id)) }`

The pool account depends on the asset id. The simulation checks the derived address
against the `PoolCreated` event.

## 3. Parameters

Set in `config/polkadot.json`. Amounts are decimal strings in DOT or JAMKB. The seed and
sale values in the file are examples; the DAO decides them.

| Field | Default | Meaning and constraints |
|-------|---------|-------------------------|
| `asset.id` | 21000000 | Any free `pallet-assets` id. `build.py create` checks it is free. |
| `asset.cap_jamkb` | 21000000 | Minted once, into custody. |
| `custody_dot` | 1 | DOT for custody, at least the existential deposit (0.01 DOT). JAMKB is non-sufficient, so an account needs DOT before it can hold JAMKB. |
| `seed.dot`, `seed.jamkb` | 50000, 500000 | The pool seed. Their ratio is the opening price (0.1 DOT per JAMKB here). |
| `seed.min_dot`, `seed.min_jamkb` | = seed | `add_liquidity` fails below these. Equal to the seed, the launch fails if someone added liquidity at another price first. |
| `seller_dot_extra` | 2 | DOT for the seller above the seed: the pool setup fee (0.40194 DOT) and the existential deposit. |
| `tranche_jamkb` | 200000 | JAMKB the seller may sell. It bounds the DAO's exposure between referenda. |
| `topup_jamkb` | 100000 | JAMKB the `topup` referendum adds. |
| `sale.task_name` | jamkb-trickle-1 | The scheduler task id is `blake2_256(task_name)`. `change` and `stop` use the same id. |
| `sale.dot_per_period` | 150 | `X`, the DOT burned per period. At least the existential deposit. Keep it at or below about 0.3% of the pool's DOT, or sandwiching the sale pays off. |
| `sale.floor_dot_per_jamkb` | 0.08 | The floor. `Y = X / floor`. |
| `sale.period_relay_blocks` | 2400 | The period in relay blocks: the Asset Hub scheduler counts relay blocks. 2400 is 4 hours. |
| `sale.count` | 180 | The number of runs. 180 runs of 4 hours is 30 days. After the last run the task is gone. |
| `sale.priority` | 200 | Scheduler priority; a higher number runs later. Referendum enactments run at 63 and referenda alarms at 128, so the sale runs after both. |
| `sale.after_relay_blocks` | 1 | The first run, relative to the enactment. |
| `referendum.enactment_after_blocks` | 14400 | The Root track's minimum enactment period; a smaller value is raised to it. |

## 4. The referenda, one by one

Each referendum is one Root proposal. `build.py <step>` builds it. The referenda are
enacted in this order:

```
A. create  ──►  B. launch  ──►  sale runs every period
                                   │
                    C. change / D. top up / E. stop, as needed
```

`launch` can also be split into `seed` and `start`, as two referenda.

### A. create: create JAMKB

Skip this step once `pallet-jamkb` is deployed; its `initialize` creates the asset (see
section 10).

`utility.batch_all` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `balances.force_transfer(treasury, custody, custody_dot)` | Custody needs DOT to hold a non-sufficient asset. |
| 2 | `assets.force_create(asset_id, custody, false, 1)` | Creates JAMKB. Custody becomes owner, issuer, admin and freezer. Non-sufficient, `min_balance` 1 plank. |
| 3 | `assets.force_set_metadata(asset_id, "JAMKB", "JAMKB", 3, false)` | Name, symbol, 3 decimals. |
| 4 | `utility.dispatch_as_fallible(Signed(custody), assets.mint(asset_id, custody, cap))` | Mints the cap into custody. Custody is the issuer, so the mint is dispatched as custody. |

After enactment: `Assets.Issued` of the cap to custody, and custody holds 21,000,000 JAMKB.

### B. launch: seed the pool and start the sale

One referendum, so that the sale never runs without its pool. `utility.batch_all` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `balances.force_transfer(treasury, seller, seed.dot + seller_dot_extra)` | The seed DOT, the pool setup fee and the existential deposit. It must come before the grant: the seller needs DOT to hold JAMKB. |
| 2 | `utility.dispatch_as_fallible(Signed(custody), assets.transfer(asset_id, seller, seed.jamkb + tranche))` | The grant. With `pallet-jamkb` this is `jamkb.grant(seller, amount)`. |
| 3 | `utility.dispatch_as(Signed(seller), asset_conversion.create_pool(DOT, JAMKB))` | Creates the pool. `dispatch_as` succeeds even when the call fails, so a pool that someone created first does not fail the referendum. |
| 4 | `utility.dispatch_as_fallible(Signed(seller), asset_conversion.add_liquidity(DOT, JAMKB, seed.dot, seed.jamkb, seed.min_dot, seed.min_jamkb, pool))` | Seeds the pool. The pool share goes to the pool account itself, where only Root can reach it. |
| 5 | `scheduler.schedule_named_after(task_id, after, Some((period, count)), priority, sale_call)` | Schedules the sale. |

`sale_call`, run by the scheduler with the Root origin every period:

```
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
- The sale call is 164 bytes, over the 128-byte inline limit. The scheduler stores it as a
  preimage itself; nothing else is needed.
- No retry is set. A retry would sell twice in one period.

After enactment:

- `AssetConversion.LiquidityAdded` with `mint_to` the pool account;
- `Scheduler.Scheduled`;
- the pool holds the seed;
- the seller holds the tranche and about 1.2 DOT;
- `Scheduler.Lookup(task_id)` gives the first run.

### C. change: new rate or floor

Edit `sale` in the config and keep `task_name`. `utility.force_batch` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `scheduler.cancel_named(task_id)` | Removes the running schedule. |
| 2 | `scheduler.schedule_named_after(task_id, …)` | The new schedule, under the same id. |

`force_batch`, not `batch_all`: if the task is already gone (its count ran out, or the
scheduler dropped it), `cancel_named` fails with `Scheduler.NotFound`. The new schedule must
still be set.

### D. top up: more JAMKB for the seller

`utility.dispatch_as_fallible(Signed(custody), assets.transfer(asset_id, seller, topup))`,
or `jamkb.grant` with `pallet-jamkb`.

### E. stop: stop the sale and return the tranche

`utility.force_batch` of:

| # | Extrinsic | Why |
|---|-----------|-----|
| 1 | `scheduler.cancel_named(task_id)` | Stops the sale. |
| 2 | `utility.dispatch_as_fallible(Signed(seller), assets.transfer_all(asset_id, custody, false))` | Returns all unsold JAMKB to custody. |

`force_batch`, not `batch_all`: in a `batch_all`, a `cancel_named` of a task that is gone
fails the whole referendum, and the tranche is not returned. The simulation shows both.

Pause and resume are `stop` and a new `launch`-style schedule (`start`). Nothing else
pauses the sale.

## 5. Extrinsic reference

| Extrinsic | Origin it needs | Used for |
|-----------|-----------------|----------|
| `balances.force_transfer(source, dest, value)` | Root | DOT from the treasury to custody and seller. A treasury spend can replace it. |
| `balances.burn(value, keep_alive)` | Signed (the sink) | Burns `X` DOT and lowers total issuance. |
| `assets.force_create(id, owner, is_sufficient, min_balance)` | Root | Creates JAMKB. |
| `assets.force_set_metadata(id, name, symbol, decimals, is_frozen)` | Root | Sets JAMKB metadata. |
| `assets.mint(id, beneficiary, amount)` | Signed (the issuer, custody) | Mints the cap. |
| `assets.transfer(id, target, amount)` | Signed (custody) | The grant and the top-up. |
| `assets.transfer_all(id, dest, keep_alive)` | Signed (seller) | Returns the tranche. |
| `asset_conversion.create_pool(asset1, asset2)` | Signed (seller); charges 0.40194 DOT | Creates the JAMKB/DOT pool. |
| `asset_conversion.add_liquidity(asset1, asset2, amount1_desired, amount2_desired, amount1_min, amount2_min, mint_to)` | Signed (seller) | Seeds the pool; the share is minted to the pool account. |
| `asset_conversion.swap_tokens_for_exact_tokens(path, amount_out, amount_in_max, send_to, keep_alive)` | Signed (seller) | The sale: exactly `X` DOT out for at most `Y` JAMKB in. |
| `scheduler.schedule_named_after(id, after, maybe_periodic, priority, call)` | Root | Runs the sale every `period`, `count` times, with the Root origin. |
| `scheduler.cancel_named(id)` | Root | Stops the sale. Fails with `NotFound` when the task is gone. |
| `utility.dispatch_as(as_origin, call)` | Root | Dispatches as a keyless account; reports an error in `DispatchedAs` but succeeds. |
| `utility.dispatch_as_fallible(as_origin, call)` | Root | Dispatches as a keyless account; an error fails the call. |
| `utility.batch_all(calls)` | any | All or nothing. |
| `utility.force_batch(calls)` | any | Runs every call; failures do not stop the rest. |
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

Check that the asset id is free on [Subscan](https://assethub-polkadot.subscan.io) or in
polkadot.js (`assets.asset(id)` is empty). Check that the `task_name` is not in use
(`scheduler.lookup(blake2_256(task_name))` is empty). `build.py` checks both.

### 6.3 Build each proposal

```bash
.venv/bin/python build.py --config config/polkadot.json create
.venv/bin/python build.py --config config/polkadot.json launch
```

Each writes `out/<step>.json`:

- `call_data`: the proposal, SCALE-encoded;
- `preimage_hash`, `preimage_len`: what the referendum names;
- `call`: the decoded call tree, to review.

`build.py` reads the chain and prints warnings: a changed runtime, a taken asset id, a
treasury too small for the seed, a pool that holds liquidity already, a sale above 0.3% of
the pool. `launch` warns that the asset does not exist until `create` is enacted. That is
expected; build `launch` again against a fork where `create` was rehearsed (next step).

Review `call` in the JSON, or paste `call_data` into polkadot.js Apps (Developer →
Extrinsics → Decode) on Asset Hub Polkadot.

### 6.4 Rehearse the exact proposal on a fork

`rehearse.py` enacts the exact bytes of `out/<step>.json` on a fork of the live chain, as an
approved Root referendum would:

```bash
./fork.sh                                                        # fresh fork of the live chain
.venv/bin/python build.py    --config config/polkadot.json create
.venv/bin/python rehearse.py --config config/polkadot.json out/create.json
.venv/bin/python build.py    --config config/polkadot.json launch --rpc ws://127.0.0.1:8000
.venv/bin/python rehearse.py --config config/polkadot.json out/launch.json
```

Each step must print `Ok` and the expected state. For example, after `launch`:

```
custody 20,300,000 JAMKB, seller 200,000 JAMKB and 1.19774 DOT,
pool 50,000 DOT / 500,000 JAMKB (price 0.10000), sale task next run at relay …
```

`launch` built against the fork has the same bytes as `launch` built against Polkadot. Only
the warnings differ.

### 6.5 Submit

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
export SURI="<the proposer's mnemonic>"     # never commit it
.venv/bin/python submit.py --config config/polkadot.json note   out/create.json   # dry run
.venv/bin/python submit.py --config config/polkadot.json --execute note   out/create.json
.venv/bin/python submit.py --config config/polkadot.json --execute submit out/create.json
.venv/bin/python submit.py --config config/polkadot.json --execute deposit <referendum index>
```

Without `--execute`, `submit.py` prints the extrinsic and its fee and sends nothing. The
preimage and the referendum can also be submitted from polkadot.js Apps or Subsquare with
the `call_data`.

Submit `launch` only after `create` is enacted. Building it earlier is fine, since its bytes
do not depend on chain state. But the treasury balance and the pool must be checked again
close to enactment: `python build.py launch` repeats the checks.

### 6.6 After each enactment

Check the events of the enactment block (`Scheduler.Dispatched` with `Ok` for the
referendum), then:

- after `create`: `assets.asset(id)` exists with custody as owner, and custody holds the cap;
- after `launch`:
  - `assetConversion.LiquidityAdded` with `mint_to` the pool account;
  - `scheduler.lookup(task_id)` is set;
  - the first run, at `after` relay blocks, shows `Balances.Burned` of `X` from the sink.

### 6.7 Before `count` runs out

The task stops after `count` runs (30 days with the defaults). A `change` referendum must
be enacted before then, with the same `task_name`. A Root referendum takes about a month
(decision period, confirmation and enactment), so submit it soon after `launch`. Otherwise
the sale pauses until it passes.

## 7. Monitoring and failure modes

```bash
.venv/bin/python monitor.py --config config/polkadot.json
```

It prints every run of the task with its result, the DOT burned, the pool price and the
JAMKB left with the seller. It prints an `ALERT` line when:

- the task fails for good: `PeriodicFailed`, `CallUnavailable` or `PermanentlyOverweight`;
- the task is no longer scheduled: it was cancelled, it ran its last run, or the scheduler
  dropped it.

| What happens | Effect | What to do |
|--------------|--------|------------|
| Price below the floor | The run fails with `AssetConversion.ProvidedMaximumNotSufficientForSwap`. Nothing is sold or burned. The task stays scheduled. | Nothing, or a `change` with a lower floor. |
| The tranche is sold out | The swap fails with a balance error every period. | `topup`, or `stop`. |
| The next slot already holds 50 scheduled tasks | The scheduler drops the task for good (`PeriodicFailed`). Anyone can fill a chosen relay block with referendum alarms. | `change` (it re-creates the task). |
| A runtime upgrade changes a call index or the `Location` encoding | The stored sale call no longer decodes; `CallUnavailable`, the task is dropped. | Rebuild with the new metadata; `change`. |
| `count` runs out | The task is gone. | `change` before it runs out (6.7). |
| Higher-priority scheduled work fills the block | The sale runs later; the cadence drifts. The next run is one period after the run, not after the slot. | Nothing. |
| Someone sends DOT to the sink | The burn still burns `X`; the extra stays in the sink. | Nothing. |
| Someone adds liquidity before `launch` | `add_liquidity` fails below the seed minimums, and the launch fails as a whole. | Check the pool before enactment; resubmit. |

## 8. Simulation

The simulation runs every referendum on a local fork of Asset Hub Polkadot. It uses the
live runtime and live state; nothing is sent to Polkadot. It enacts each proposal as an
approved Root referendum is enacted: the preimage is noted by a signed account, then a
scheduler entry with the Root origin dispatches it. The proposals are the ones `build.py`
produces. Only the period is shorter, 8 relay blocks instead of 2400, so that a sale runs
every second block.

### 8.1 Setup

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

### 8.2 Run

```bash
./fork.sh                                                  # fresh fork on ws://127.0.0.1:8000
.venv/bin/python simulate.py --config config/simulation.json

./fork.sh
.venv/bin/python simulate.py --config config/simulation.json --no-precreate

pkill -f chopsticks                                        # stop the fork
```

Each run takes a few minutes. It prints every referendum, every sale and a `PASS` or
`FAIL` per check, and exits with status 1 if a check fails. `docs/sample-simulation-output.txt`
is a full passing run.

### 8.3 What it checks

| Step | Checked |
|------|---------|
| A. create | The cap is minted into custody. |
| Someone creates the pool first (skipped with `--no-precreate`) | The derived pool account equals the one in `PoolCreated`. |
| B. launch | It succeeds with or without an existing pool. The pool holds the seed. The pool share is held by the pool account. The seller holds the tranche. The task is scheduled. |
| Periods 1 and 2 | Each burns exactly `X` and sells at most `Y`. The sink is reaped. |
| C. change (X to 100 DOT, same id) | The next period burns the new `X`. |
| A holder dumps JAMKB under the floor | The run fails with `ProvidedMaximumNotSufficientForSwap`. Nothing is burned. The task stays scheduled. |
| The holder buys back | The next period sells again. |
| D. top up | The seller receives the top-up. |
| E. stop | The task is cancelled. The unsold tranche returns to custody. No sale runs after. |
| F. a sale with `count` 2 | It runs exactly twice, then the task is gone. |
| G. stop in `batch_all` once the task is gone | Fails with `Scheduler.NotFound`; nothing is returned. |
| H. stop in `force_batch` once the task is gone | Succeeds; the tranche returns. |

Alice (`//Alice`) stands in for the market: she notes the preimages and trades in the pool.
She is funded by writing storage on the fork, which is the only direct storage write besides
the Root scheduler entries that stand in for passed referenda.

### 8.4 What it does not check

- The real 2400-block period and the 30-day `count`.
- A slot filled with 50 tasks, which drops the sale.
- The vote itself, and the deposits; the referendum's enactment is simulated.
- `pallet-jamkb`, which is not on Asset Hub Polkadot.

## 9. Files

| File | Purpose |
|------|---------|
| `config/polkadot.json` | Parameters for Asset Hub Polkadot. |
| `config/simulation.json` | The same, against the fork, with an 8-block period. |
| `jamkb_trickle/encoding.py` | Account, pool and task id derivations; locations. |
| `jamkb_trickle/config.py` | Loads a config and derives accounts and amounts. |
| `jamkb_trickle/calls.py` | Builds every proposal. |
| `jamkb_trickle/chain.py` | Read helpers. |
| `build.py` | Builds a proposal, checks it against the chain, writes `out/<step>.json`. Read-only. |
| `rehearse.py` | Enacts a built proposal on the fork. |
| `submit.py` | Notes the preimage, submits the referendum, places the decision deposit. Dry run by default. |
| `monitor.py` | Watches the sale and alerts when it stops. |
| `simulate.py` | The full simulation, with checks. |
| `fork.sh`, `chopsticks/` | Starts the chopsticks fork. |
| `docs/sample-simulation-output.txt` | A passing simulation run. |

## 10. Known limits

- **Without `pallet-jamkb`, the cap is policy, not code.** Custody holds the issuer role, so a
  Root referendum could mint more JAMKB. There is no pause flag and no `BudgetGranted`
  record. There is no redemption to JAM either: that needs `pallet-jamkb`.
- **Moving to `pallet-jamkb` later.** Its `initialize` rejects an asset that already exists.
  The pallet would have to take over the asset and its roles (`assets.transfer_ownership` and
  `assets.set_team` dispatched as custody) and the custody balance. After that, set
  `"use_pallet_jamkb": true` in the config: grants become `jamkb.grant`. The other calls stay
  the same.
- **Root only.** Every change, including an emergency stop, is a Root referendum. The
  tranche and the `count` bound what a sale can do between referenda.
- **No catching up.** A period that sells nothing is lost.
- **Proceeds.** Each period burns `X` DOT. In a quiet market that DOT is the seed coming back,
  not buyers' DOT. Buyers' DOT stays in the pool, and the pool fee accrues to the locked
  pool share.
