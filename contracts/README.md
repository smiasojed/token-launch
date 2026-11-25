# JamkbTrickle: Option 1 as a contract

Solidity contracts for Asset Hub (pallet-revive). They sell the DAO's JAMKB budget into the
DOT/JAMKB pool at a daily rate and burn the DOT they receive. This is Option 1 of the PRD. The
scheduled-call setup in the parent folder is Option 3.

- `JamkbTrickle`: the plain trickle.
- `JamkbTrickleOpening`: the trickle plus an opening phase that finds the price. It extends
  `JamkbTrickle` and behaves like it outside the opening. See [Opening phase](#opening-phase).

## How it works

- `trickle()` can be called by anyone. It sells the JAMKB accrued since the last sale and burns
  the contract's whole DOT balance, so DOT that reached the contract any other way is burned too.
  A second call in the same block sells nothing. Calls that sell nothing do not revert; they emit
  `Skipped` with the reason: `Paused`, `NothingAccrued`, `NoPool`, `NoBudget`, `BelowMinPrice`,
  `CapZero`, `QuoteFailed` or `SwapFailed`. Only a failed burn reverts the call.
- `preview()` returns what the next call would do (amount, cap, reserves, quote, minimum, skip
  reason), for monitoring. `Trickled` carries the reserves before the sale.
- An hourly scheduler task calls it with `revive.call`. Anyone else can call it too, so a
  dropped task does not stop the sale; the next call sells what accrued, up to the cap.
- Each sale sells the smallest of: the accrual (`ratePerDay` per day), `capBps` of the pool's
  JAMKB, and the budget. Accrual above the cap or the budget is dropped. The cap uses the smaller
  of the pool's JAMKB now and at the last sale (or `resume`), so selling JAMKB into the pool just
  before a sale does not raise it.
- Keep `capBps` at or below the pool's fee in basis points (30 for 0.3%). A sale of up to that
  share of the pool cannot be sandwiched at a profit; above it, it can. Pools can have their own
  fee, so check the pool's fee before setting the cap.
- A sale whose price is below `minPrice`, or whose quote or swap fails, is skipped. Its accrual
  is kept for a later call, still bounded by the cap. A failed swap leaves no state change: the
  swap runs before the sale's state is written (the precompile cannot call back). `minPrice` 0
  means no floor; the sale then asks for at least one plank, since the pallet rejects a zero
  minimum.
- The sub-unit part of the accrual is carried to the next sale, so calling every block sells the
  same amount as calling once an hour.
- Root only, checked with `callerIsRoot()` of the system precompile: `setRate`, `setMinPrice`,
  `setCap`, `pause`, `resume`, `returnBudget(to)`, `burnBalance()`. A referendum calls them with
  `revive.call` and no value. The contract starts paused; `resume` starts the sale. `ratePerDay`
  and `minPrice` are bounded by `2^128 - 1`, so the sale math cannot overflow.

## Opening phase

The seed sets the opening price, and it may be wrong. `JamkbTrickleOpening` measures how much
JAMKB buyers take and sells faster only while they take less than the base release. It follows
the PRD's opening phase (buys against 90% of the release, an opening budget, bounded restarts),
with the changes an economic review recommended.

1. **Measure**: from the start it sells at the base rate and counts net buys. From `window`
   (3 days) after the start, it reads a signal at every sale.
2. **Signal**: demand when net buys over the trailing window reach `thresholdBps` (90%) of the
   base release, no demand when they stay below it. A signal counts only when the window shows it
   at this sale and at the previous one, so a trade bought and sold back around a single sale
   cannot set it. A decision needs the same signal at two sales at least `confirmGap` (1 day)
   apart.
3. **Measuring ends** with confirmed demand (the seed price holds, the opening ends, no boost)
   or confirmed no demand (the boost starts). With the example values this is at day 4.
4. **Boost**: the rate is `boostBpsPerDay` of the pool's JAMKB a day (480 = 4.8%, 8x the base
   rate at this seed, 0.2% of the pool per hourly sale, under the cap), never below the base rate.
   The JAMKB sold above the base accrual is bounded exactly by `extraBudget`, over all openings.
   No demand at two sales in a row clears a pending demand signal.
5. **End**: confirmed demand, the deadline, Root with `endOpening`, or `pause`. Root starts an
   opening with `startOpening(duration)` while the sale runs, the pool exists and no opening
   runs, at most `maxStarts` times in all. `endOpening` with nothing open does nothing. A passed
   deadline is settled at the next sale or Root call.

**Net buys from the price.** Between two sales the contract computes the JAMKB bought as
`J × (1 − sqrt(P_then / P_now))`, with `J` and `P_then` the pool's JAMKB and price right after
the earlier sale. This is exact for swaps on a constant-product pool, up to the fee. Adding or
removing liquidity leaves the price unchanged and is not counted; buying and selling back nets
out; the contract's own sales are not counted. The baseline is taken at `startOpening`, so buys
before the first sale count. Buys over a gap between sales longer than the window are scaled down
to the window. The window is kept in 12 buckets, and the threshold compares against the release
over exactly the time the buckets cover, at the current `ratePerDay`.

**How much must be sold to find a lower price.** The pool price falls with the square of the
pool's JAMKB. Finding a price `r` times the seed price takes selling `1/sqrt(r) - 1` of the
pool's JAMKB: 11.8% for 20% lower, 41% for half. Set `extraBudget` from the largest correction
the DAO accepts: 60,000 JAMKB allows 20% on this pool, 100,000 allows 31%. A higher market price
needs no selling; buyers lift the price themselves. The seed size is the first lever: half the
seed halves the JAMKB needed for any correction, with bigger price swings.

**Not bounded**: nothing but `minPrice` and the extra budget bounds how far the price falls during
the boost.

Measured on the contract (pool of 50,000 DOT and 500,000 JAMKB, 125 JAMKB an hour, buyers who take
everything below the market price):

| Case | Result |
| --- | --- |
| Market at the seed price | ended at hour 96, no extra sold |
| Market 20% above | ended without a boost, no extra sold |
| Market 20% below | price found in 138 hours, ended at hour 173, 74,000 extra JAMKB sold (part of it to buyers at the market price while demand was confirmed) |
| No buyers | boost at hour 96, extra budget used exactly, then the base rate |
| No sale for the whole measuring window, buyers active | buys counted, ended without a boost |

Attacks:

| Attack | Result |
| --- | --- |
| Call every block and buy back each drop | does not end the boost (it ended the earlier per-sale design in 27 blocks) |
| Buy, sale, sell back in one transaction | sets no signal; two such trades a day apart do not end the opening |
| Sell, sale, buy back at the measuring end | does not start a boost in a correctly priced market |
| Flash sells while demand is being confirmed | do not reset it; the opening ends and the extra budget is not drained |
| Large buy sold back within the day | does not end the boost |
| Large buy held for a day | ends it from about 800 DOT (8,200 JAMKB) held while the boost lowers the price; 600 DOT does not |
| Not buying | free, the boost runs; bounded by the extra budget and `minPrice` |

Example configuration: `window 259200` (3 days), `thresholdBps 9000`, `boostBpsPerDay 480`,
`confirmGap 86400`, `extraBudget` from the formula above, `maxStarts 3`. The boost needs a call at
least every 1.5 hours, or the cap cuts the sale; keep the hourly scheduler task.

## Units

| Parameter | Unit | Example |
| --- | --- | --- |
| `ratePerDay` | JAMKB base units (3 decimals) per day | `3000000` = 3,000 JAMKB a day, 125 an hour |
| `minPrice` | DOT planks per JAMKB base unit, times 1e18 | `8e23` = 0.08 DOT per JAMKB |
| `capBps` | share of the pool's JAMKB per sale, basis points | `30` = 0.3% |
| `dotAsset` | SCALE `Location` | `0x0100` |
| `jamkbAsset` | SCALE `Location` | `0x000204320502bd0105` for asset 21000000 |

## Precompile addresses

The constructor takes them, so they are not hardcoded. On Asset Hub Westend in polkadot-sdk:

- asset conversion: `0x0000000000000000000000000000000004200000`
- system: `0x0000000000000000000000000000000000000900` (built-in precompiles sit at their raw
  number; only external ones, like asset conversion, are shifted)
- ERC20 of a trust-backed asset: the asset id in the first 4 bytes, then
  `000000000000000000000000` and `01200000`. For 21000000:
  `0x01406f40000000000000000000000000_01200000` without the underscore.

## Assumptions and gaps

- **Burn**: `ISystem.burn(uint256)` does not exist yet. The contract assumes it burns from the
  calling contract, lowers total issuance, and takes the unit of `address(this).balance`: the
  18-decimal native value of revive, which excludes the existential deposit and holds. The
  contract burns its whole balance and reverts if anything is left. Today revive's own burn path
  (`burn_with_dust`) sends DOT to `T::OnBurn`, which Asset Hub Westend sets to the DAP buffer, so
  the final precompile must be checked to lower issuance as intended.
- **Asset conversion precompile**: wired into Asset Hub Westend here. Whether Asset Hub Polkadot
  has it is not checked.
- **Custody address**: `returnBudget` takes an H160. A keyless custody account needs a mapped
  address (`revive.map_account` dispatched as custody) for the JAMKB to arrive at that account.
- **Timing** uses `block.timestamp`, so the daily rate does not change with the block time.
- **Deployment**: a signed account deploys (`InstantiateOrigin` is signed), so the code hash
  alone does not prove the configuration. The referendum that funds and resumes the contract
  must check `configHash()` against the expected parameters, and the build (resolc version and
  flags) must be reproducible. The constructor rejects precompile addresses with no code.
- **Upgrade**: there is no upgrade path in the contract. A Root `revive.set_code` keeps storage
  and balance; keep the storage layout if that path is used. Otherwise deploy anew and
  `returnBudget`.
- **Storage deposits**: Root calls do not pay storage deposits; slots they create are not backed.
  The amounts are tiny; fund the contract's deposit with some margin.
- **Scheduler weight**: the hourly `revive.call` needs a weight limit. Set it from a dry run of
  the boost path with a 2 to 3x margin; a run that runs out of weight is not retried until the
  next period.
- **Frozen asset**: if JAMKB or the contract's account is frozen, the swap fails and the sale is
  skipped with `SwapFailed`; `returnBudget` fails until thawed.
- **Not tested on a chain**: the tests use mock precompiles. Not covered: Root calls through
  `revive.call`, storage deposits, the real precompiles, weights.
- **Opening phase**: differs from the PRD in the window (3 days, not 7), measuring before the
  boost, decisions confirmed at two sales a day apart, and net buys from the price instead of
  the reserves. `windowRelease` assumes the base rate ran for the whole window; a `setRate`
  during an opening changes the reference at once.

## Build and test

Needs Foundry and `resolc`.

```text
cd contracts
forge test --offline                       # 51 tests against mock precompiles
resolc --bin -O3 src/JamkbTrickle.sol -o out --overwrite          # PolkaVM bytecode
resolc --bin -O3 src/JamkbTrickleOpening.sol -o out --overwrite
```

## Files

```text
src/JamkbTrickle.sol              the plain trickle
src/JamkbTrickleOpening.sol       the trickle with the opening phase
src/interfaces/IAssetConversion.sol
src/interfaces/IERC20.sol
src/interfaces/ISystem.sol        callerIsRoot (exists) and burn (assumed)
test/Mocks.sol                    mock system, token and constant-product pool
test/JamkbTrickle.t.sol           tests of the plain trickle
test/JamkbTrickleOpening.t.sol    tests of the opening: signal, phases, market scenarios, attacks
foundry.toml
```
