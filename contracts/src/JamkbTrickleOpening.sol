// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {JamkbTrickle} from "./JamkbTrickle.sol";
import {IAssetConversion} from "./interfaces/IAssetConversion.sol";
import {IERC20} from "./interfaces/IERC20.sol";
import {ISystem} from "./interfaces/ISystem.sol";

/// The trickle with an opening phase that finds the price (PRD Option 1, step 4).
///
/// The seed sets the opening price, and it may be wrong. The opening measures how much JAMKB
/// buyers take, and sells faster only while they take less than the base release:
///
/// 1. Measure: from the start the contract sells at the base rate and counts net buys. From
///    `window` after the start it reads the signal at every sale.
/// 2. Signal: demand when net buys over the trailing window reach `thresholdBps` of the base
///    release, no demand when they stay below it. A signal counts only when it holds at this sale
///    and the previous one, so a trade bought and sold back around one sale cannot set it. A
///    decision needs the same signal at two sales at least `confirmGap` apart.
/// 3. Measuring ends with confirmed demand (the seed price holds, the opening ends) or confirmed
///    no demand (the boost starts).
/// 4. Boost: the rate is `boostBpsPerDay` of the pool's JAMKB a day, never below the base rate.
///    The JAMKB sold above the base accrual is bounded by `extraBudget`, over all openings. It
///    ends with confirmed demand, at the deadline, by Root, or when the sale is paused.
///
/// Net buys are computed from the pool price, not the reserves: for a constant-product pool,
/// the JAMKB bought between two sales is `J × (1 − sqrt(P_then / P_now))`, where `J` and `P_then`
/// are the pool's JAMKB and price right after the earlier sale. Adding or removing liquidity
/// leaves the price unchanged, so it does not count. Selling back what was bought nets out.
/// The contract's own sales are not counted.
contract JamkbTrickleOpening is JamkbTrickle {
	struct OpeningConfig {
		/// Length of the trailing window and of the measuring time, in seconds.
		uint256 window;
		/// Net buys over the window, as a share of the base release, that count as demand.
		uint256 thresholdBps;
		/// Boosted rate, as a share of the pool's JAMKB per day, in basis points.
		uint256 boostBpsPerDay;
		/// Least time between the two sales that must show the same signal for a decision.
		uint256 confirmGap;
		/// JAMKB base units the openings may sell above the base accrual, in all.
		uint256 extraBudget;
		/// Most openings Root may start, the first one included.
		uint256 maxStarts;
	}

	enum Phase {
		Off,
		Measuring,
		Boosting
	}

	enum Signal {
		None,
		Demand,
		NoDemand
	}

	enum End {
		Demand,
		Deadline,
		Root,
		Paused
	}

	/// The window is kept in this many buckets.
	uint256 public constant BUCKETS = 12;

	OpeningConfig public config;
	Phase public phase;
	uint256 public openingStart;
	uint256 public measureEnd;
	uint256 public openingEnd;
	/// The signal waiting for confirmation, and the sale that first showed it.
	Signal public pending;
	uint256 public pendingSince;
	/// Net buys over the window as read at the previous sale.
	int256 public prevWindowBuys;
	uint256 public extraSold;
	uint256 public starts;
	/// Pool price and JAMKB after the previous sale of this opening, or at its start, and when.
	/// The price is in the unit of `minPrice`.
	uint256 public lastPriceAfter;
	uint256 public lastJamkbAfter;
	uint256 public lastBaselineAt;

	int256[BUCKETS] private bucketBuys;
	uint256[BUCKETS] private bucketEpoch;

	event OpeningStarted(uint256 measureEnd, uint256 openingEnd);
	event BoostStarted(int256 windowBuys);
	event NetBuys(int256 sinceLastSale, int256 window, Signal signal);
	event SignalPending(Signal signal);
	event OpeningEnded(End reason);

	error InvalidOpening();
	error NoStartsLeft();
	error OpeningRunning();

	constructor(
		IAssetConversion dex_,
		ISystem system_,
		IERC20 jamkb_,
		bytes memory dotAsset_,
		bytes memory jamkbAsset_,
		uint256 ratePerDay_,
		uint256 minPrice_,
		uint256 capBps_,
		OpeningConfig memory config_
	) JamkbTrickle(dex_, system_, jamkb_, dotAsset_, jamkbAsset_, ratePerDay_, minPrice_, capBps_) {
		if (
			config_.window < BUCKETS || config_.thresholdBps == 0 || config_.thresholdBps > BPS ||
				config_.boostBpsPerDay == 0 || config_.boostBpsPerDay > BPS ||
				config_.confirmGap == 0 || config_.confirmGap >= config_.window ||
				config_.maxStarts == 0
		) revert InvalidOpening();
		config = config_;
	}

	function inOpening() public view returns (bool) {
		return phase != Phase.Off && block.timestamp < openingEnd;
	}

	function configHash() public view override returns (bytes32) {
		return keccak256(abi.encode(super.configHash(), config));
	}

	/// Starts an opening of `duration` seconds, measuring first. Needs the sale running, the
	/// pool, and no opening running.
	function startOpening(uint256 duration) external onlyRoot {
		_settleDeadline();
		if (phase != Phase.Off) revert OpeningRunning();
		if (starts >= config.maxStarts) revert NoStartsLeft();
		if (paused || duration <= config.window + config.confirmGap) revert InvalidOpening();
		uint256 poolDot;
		uint256 poolJamkb;
		try dex.getReserves(dotAsset, jamkbAsset) returns (uint256 d, uint256 r) {
			(poolDot, poolJamkb) = (d, r);
		} catch {
			revert InvalidOpening();
		}
		if (poolDot == 0 || poolJamkb == 0) revert InvalidOpening();
		starts += 1;
		phase = Phase.Measuring;
		openingStart = block.timestamp;
		measureEnd = block.timestamp + config.window;
		openingEnd = block.timestamp + duration;
		pending = Signal.None;
		pendingSince = 0;
		prevWindowBuys = 0;
		// The baseline is taken now, so buys before the first sale count.
		lastPriceAfter = poolDot * PRICE_SCALE / poolJamkb;
		lastJamkbAfter = poolJamkb;
		lastBaselineAt = block.timestamp;
		for (uint256 i = 0; i < BUCKETS; i++) {
			bucketBuys[i] = 0;
			bucketEpoch[i] = 0;
		}
		emit OpeningStarted(measureEnd, openingEnd);
	}

	/// Ends a running opening. Does nothing when none is running.
	function endOpening() external onlyRoot {
		_settleDeadline();
		if (phase != Phase.Off) _end(End.Root);
	}

	/// Pausing the sale ends a running opening; Root starts a new one after resuming.
	function pause() public override {
		super.pause();
		_settleDeadline();
		if (phase != Phase.Off) _end(End.Paused);
	}

	/// Net JAMKB bought over the trailing window, as recorded at sales. The window is the current
	/// bucket and the `BUCKETS - 1` before it, since the opening started.
	function windowBuys() public view returns (int256 sum) {
		uint256 epoch = block.timestamp / _bucketLength();
		for (uint256 i = 0; i < BUCKETS; i++) {
			if (bucketEpoch[i] + BUCKETS > epoch) sum += bucketBuys[i];
		}
	}

	/// The base release over the time `windowBuys` covers, the reference for the threshold.
	function windowRelease() public view returns (uint256) {
		return ratePerDay * _covered() / 1 days;
	}

	function _covered() private view returns (uint256 covered) {
		uint256 len = _bucketLength();
		covered = (BUCKETS - 1) * len + block.timestamp % len;
		uint256 sinceStart = block.timestamp - openingStart;
		if (sinceStart < covered) covered = sinceStart;
	}

	function _bucketLength() private view returns (uint256) {
		return config.window / BUCKETS;
	}

	function _boosting() private view returns (bool) {
		return phase == Phase.Boosting && block.timestamp < openingEnd &&
			extraSold < config.extraBudget;
	}

	function _currentRate() internal view override returns (uint256) {
		if (!_boosting()) return ratePerDay;
		uint256 boosted = refReserve * config.boostBpsPerDay / BPS;
		return boosted > ratePerDay ? boosted : ratePerDay;
	}

	/// Keeps the JAMKB sold above the base accrual within `extraBudget`.
	function _limitSale(uint256 amount) internal view override returns (uint256) {
		uint256 extra = _extraIn(amount);
		uint256 left = config.extraBudget - extraSold;
		return extra > left ? amount - (extra - left) : amount;
	}

	/// The part of `amount` above what the base rate accrued since the last sale. Valid while
	/// `lastTrickle` is still the previous sale's, which holds in `_limitSale` and `_onSale`.
	function _extraIn(uint256 amount) private view returns (uint256) {
		if (!_boosting()) return 0;
		uint256 base = ratePerDay * (block.timestamp - lastTrickle) / 1 days;
		return amount > base ? amount - base : 0;
	}

	function _onSale(uint256 sold, uint256 poolDot, uint256 poolJamkb, uint256 dotOut)
		internal
		override
	{
		if (phase == Phase.Off) return;
		if (_settleDeadline()) return;

		uint256 extra = _extraIn(sold);
		uint256 left = config.extraBudget - extraSold;
		extraSold += extra < left ? extra : left;

		uint256 priceNow = poolDot * PRICE_SCALE / poolJamkb;
		int256 net = _netBuys(lastJamkbAfter, lastPriceAfter, priceNow);
		// Buys over a gap longer than the window are scaled to the window, so days of buys do not
		// land in one bucket at full weight.
		uint256 gap = block.timestamp - lastBaselineAt;
		uint256 covered = _covered();
		if (gap > covered) net = net * int256(covered) / int256(gap);
		_record(net);
		lastPriceAfter = (poolDot - dotOut) * PRICE_SCALE / (poolJamkb + sold);
		lastJamkbAfter = poolJamkb + sold;
		lastBaselineAt = block.timestamp;

		int256 current = windowBuys();
		Signal signal = _signal(current);
		prevWindowBuys = current;
		emit NetBuys(net, current, signal);
		if (phase == Phase.Measuring && block.timestamp < measureEnd) return;
		_decide(signal);
	}

	/// The signal holds only if this sale and the previous one agree, so one trade bought and
	/// sold back around a single sale cannot set it.
	function _signal(int256 current) private view returns (Signal) {
		int256 threshold = int256(windowRelease() * config.thresholdBps / BPS);
		int256 low = current < prevWindowBuys ? current : prevWindowBuys;
		int256 high = current < prevWindowBuys ? prevWindowBuys : current;
		if (low >= threshold) return Signal.Demand;
		if (high < threshold) return Signal.NoDemand;
		return Signal.None;
	}

	function _decide(Signal signal) private {
		if (signal == Signal.None) return;
		if (signal != pending) {
			// In the boost, only demand waits for confirmation; no demand just clears it.
			if (phase == Phase.Boosting && signal == Signal.NoDemand) {
				pending = Signal.None;
				pendingSince = 0;
				return;
			}
			pending = signal;
			pendingSince = block.timestamp;
			emit SignalPending(signal);
			return;
		}
		if (block.timestamp - pendingSince < config.confirmGap) return;
		if (signal == Signal.Demand) return _end(End.Demand);
		phase = Phase.Boosting;
		pending = Signal.None;
		pendingSince = 0;
		emit BoostStarted(prevWindowBuys);
	}

	/// Ends an opening whose deadline has passed. Returns true if it did.
	function _settleDeadline() private returns (bool) {
		if (phase == Phase.Off || block.timestamp < openingEnd) return false;
		_end(End.Deadline);
		return true;
	}

	/// JAMKB bought from a constant-product pool while its price moved from `priceThen` to
	/// `priceNow`, starting from `jamkbThen`: `jamkbThen × (1 − sqrt(priceThen / priceNow))`.
	/// Negative when the price fell, that is when holders sold.
	function _netBuys(uint256 jamkbThen, uint256 priceThen, uint256 priceNow)
		private
		pure
		returns (int256)
	{
		uint256 root = _sqrt(priceThen * 1e36 / priceNow); // sqrt(ratio) times 1e18
		if (root <= 1e18) return int256(jamkbThen * (1e18 - root) / 1e18);
		return -int256(jamkbThen * (root - 1e18) / 1e18);
	}

	function _record(int256 net) private {
		uint256 epoch = block.timestamp / _bucketLength();
		uint256 i = epoch % BUCKETS;
		if (bucketEpoch[i] != epoch) {
			bucketEpoch[i] = epoch;
			bucketBuys[i] = 0;
		}
		bucketBuys[i] += net;
	}

	function _end(End reason) private {
		phase = Phase.Off;
		pending = Signal.None;
		pendingSince = 0;
		emit OpeningEnded(reason);
	}

	function _sqrt(uint256 x) private pure returns (uint256 r) {
		if (x == 0) return 0;
		r = 1 << (_log2(x) / 2 + 1);
		for (uint256 i = 0; i < 8; i++) r = (r + x / r) / 2;
		uint256 down = x / r;
		if (down < r) r = down;
	}

	function _log2(uint256 x) private pure returns (uint256 n) {
		while (x > 1) {
			x >>= 1;
			n++;
		}
	}
}
