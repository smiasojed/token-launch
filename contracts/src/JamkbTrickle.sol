// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IAssetConversion} from "./interfaces/IAssetConversion.sol";
import {IERC20} from "./interfaces/IERC20.sol";
import {ISystem} from "./interfaces/ISystem.sol";

/// Sells the DAO's JAMKB budget into the DOT/JAMKB pool at a daily rate and burns the DOT it
/// receives.
///
/// Anyone may call `trickle()`. It sells what has accrued since the last sale, so a second call
/// in the same block sells nothing. A scheduler task can call it every hour, and anyone else can
/// call it too, so the sale does not depend on the task.
///
/// Root, through a referendum, sets the rate, the minimum price and the cap, pauses and resumes
/// the sale, returns the budget and burns stray DOT. The contract starts paused.
contract JamkbTrickle {
	/// Scale of `minPrice`.
	uint256 public constant PRICE_SCALE = 1e18;
	/// Basis points, the unit of `capBps`.
	uint256 public constant BPS = 10_000;
	/// Upper bound of `ratePerDay` and `minPrice`, so the sale math cannot overflow.
	uint256 public constant MAX_VALUE = type(uint128).max;

	IAssetConversion public immutable dex;
	ISystem public immutable system;
	/// The ERC20 precompile of the JAMKB asset.
	IERC20 public immutable jamkb;

	/// SCALE-encoded `Location` of DOT, `0x0100` on Asset Hub.
	bytes public dotAsset;
	/// SCALE-encoded `Location` of JAMKB: `(0, [PalletInstance(50), GeneralIndex(id)])`.
	bytes public jamkbAsset;

	/// JAMKB base units released per day.
	uint256 public ratePerDay;
	/// Lowest accepted price, in DOT planks per JAMKB base unit, times `PRICE_SCALE`.
	uint256 public minPrice;
	/// Most one sale may sell, as a share of the pool's JAMKB in basis points.
	uint256 public capBps;
	bool public paused;
	/// Timestamp from which the next sale counts accrual.
	uint256 public lastTrickle;
	/// Accrual below one base unit left by the last sale, in base units times seconds per day.
	uint256 public remainder;
	/// The pool's JAMKB at the last sale or at `resume`. The cap uses the smaller of this and the
	/// current reserve, so selling JAMKB into the pool just before a sale does not raise the cap.
	uint256 public refReserve;

	enum Skip {
		Paused,
		NothingAccrued,
		NoPool,
		NoBudget,
		BelowMinPrice,
		CapZero,
		QuoteFailed,
		SwapFailed
	}

	/// What the next sale would do now.
	struct Plan {
		bool ok;
		Skip reason;
		/// JAMKB to sell, and the accrual before the cap, budget and limit.
		uint256 amount;
		uint256 full;
		uint256 cap;
		uint256 poolDot;
		uint256 poolJamkb;
		/// DOT planks the sale would receive, and the least it accepts.
		uint256 quote;
		uint256 minOut;
	}

	/// `dotReceived` is in planks; the burn takes the contract's whole balance.
	event Trickled(uint256 jamkbSold, uint256 dotReceived, uint256 poolDot, uint256 poolJamkb);
	event Burned(uint256 value);
	event Skipped(Skip reason);
	event RateSet(uint256 ratePerDay);
	event MinPriceSet(uint256 minPrice);
	event CapSet(uint256 capBps);
	event Paused();
	event Resumed();
	event BudgetReturned(address to, uint256 amount);

	error NotRoot();
	error InvalidCap();
	error InvalidValue();
	error NoCode(address account);
	error BurnFailed();
	error TransferFailed();

	modifier onlyRoot() {
		if (!system.callerIsRoot()) revert NotRoot();
		_;
	}

	constructor(
		IAssetConversion dex_,
		ISystem system_,
		IERC20 jamkb_,
		bytes memory dotAsset_,
		bytes memory jamkbAsset_,
		uint256 ratePerDay_,
		uint256 minPrice_,
		uint256 capBps_
	) {
		// A wrong address would make every call to it look like a success with no data.
		if (address(dex_).code.length == 0) revert NoCode(address(dex_));
		if (address(system_).code.length == 0) revert NoCode(address(system_));
		if (address(jamkb_).code.length == 0) revert NoCode(address(jamkb_));
		if (capBps_ == 0 || capBps_ > BPS) revert InvalidCap();
		if (ratePerDay_ > MAX_VALUE || minPrice_ > MAX_VALUE) revert InvalidValue();
		dex = dex_;
		system = system_;
		jamkb = jamkb_;
		dotAsset = dotAsset_;
		jamkbAsset = jamkbAsset_;
		ratePerDay = ratePerDay_;
		minPrice = minPrice_;
		capBps = capBps_;
		paused = true;
		lastTrickle = block.timestamp;
	}

	/// JAMKB accrued since the last sale, before the cap and the budget.
	function accrued() public view returns (uint256) {
		return _scaledAccrual() / 1 days;
	}

	function _scaledAccrual() private view returns (uint256) {
		return _currentRate() * (block.timestamp - lastTrickle) + remainder;
	}

	/// What `trickle()` would do now, for monitoring.
	function preview() external view returns (Plan memory) {
		return _plan();
	}

	/// One hash over everything that defines the sale, for the referendum that enables it to
	/// compare against the expected deployment.
	function configHash() public view virtual returns (bytes32) {
		return keccak256(
			abi.encode(dex, system, jamkb, dotAsset, jamkbAsset, ratePerDay, minPrice, capBps)
		);
	}

	/// Hooks for contracts that extend the trickle. The defaults are the plain trickle.

	/// JAMKB base units per day accruing now.
	function _currentRate() internal view virtual returns (uint256) {
		return ratePerDay;
	}

	/// Lowest accepted price now, in the unit of `minPrice`.
	function _currentFloor() internal view virtual returns (uint256) {
		return minPrice;
	}

	/// Lowers the sale amount after the cap and the budget.
	function _limitSale(uint256 amount) internal view virtual returns (uint256) {
		return amount;
	}

	/// Runs after a successful swap and before the sale's state is written, so `lastTrickle`
	/// is still the previous sale's. Gets the reserves before the sale and the DOT received.
	function _onSale(uint256 sold, uint256 poolDot, uint256 poolJamkb, uint256 dotOut)
		internal
		virtual
	{}

	function _plan() private view returns (Plan memory p) {
		if (paused) return _no(p, Skip.Paused);
		p.full = accrued();
		if (p.full == 0) return _no(p, Skip.NothingAccrued);

		try dex.getReserves(dotAsset, jamkbAsset) returns (uint256 d, uint256 r) {
			(p.poolDot, p.poolJamkb) = (d, r);
		} catch {
			return _no(p, Skip.NoPool);
		}
		uint256 ref = refReserve == 0 || p.poolJamkb < refReserve ? p.poolJamkb : refReserve;
		p.cap = ref * capBps / BPS;
		uint256 budget = jamkb.balanceOf(address(this));
		if (budget == 0) return _no(p, Skip.NoBudget);
		p.amount = p.full;
		if (p.amount > p.cap) p.amount = p.cap;
		if (p.amount > budget) p.amount = budget;
		p.amount = _limitSale(p.amount);
		if (p.amount == 0) return _no(p, Skip.CapZero);

		// The pallet rejects a zero minimum, so the least accepted is one plank.
		p.minOut = _ceilDiv(p.amount * _currentFloor(), PRICE_SCALE);
		if (p.minOut == 0) p.minOut = 1;
		try dex.quoteExactTokensForTokens(jamkbAsset, dotAsset, p.amount, true) returns (
			uint256 q
		) {
			p.quote = q;
		} catch {
			return _no(p, Skip.QuoteFailed);
		}
		if (p.quote < p.minOut) return _no(p, Skip.BelowMinPrice);
		p.ok = true;
	}

	/// Sells the accrued JAMKB, at most `capBps` of the pool's JAMKB and the budget, and burns
	/// the contract's DOT. Does not revert when there is nothing to sell or the swap fails; it
	/// emits `Skipped` instead. Reverts only if the burn fails.
	///
	/// Accrual beyond the cap or the budget is dropped. A sale below the minimum price, or one
	/// whose quote or swap fails, is skipped and the accrual is kept for a later call, still
	/// bounded by the cap.
	function trickle() external returns (uint256 sold, uint256 dotOut) {
		Plan memory p = _plan();
		if (!p.ok) return _skip(p.reason);

		// Computed before `_onSale` can change the rate.
		uint256 newRemainder = p.amount == p.full ? _scaledAccrual() % 1 days : 0;
		bytes[] memory path = new bytes[](2);
		path[0] = jamkbAsset;
		path[1] = dotAsset;
		// The swap runs before the state is written so that a failed swap leaves no trace. The
		// precompile cannot call back into this contract. keepAlive false: the budget may sell
		// out to zero.
		try dex.swapExactTokensForTokens(path, p.amount, p.minOut, address(this), false) returns (
			uint256 out
		) {
			dotOut = out;
		} catch {
			return _skip(Skip.SwapFailed);
		}
		_onSale(p.amount, p.poolDot, p.poolJamkb, dotOut);
		remainder = newRemainder;
		lastTrickle = block.timestamp;
		refReserve = p.poolJamkb;
		emit Trickled(p.amount, dotOut, p.poolDot, p.poolJamkb);
		_burnAll();
		return (p.amount, dotOut);
	}

	/// Burns DOT that reached the contract outside a sale. Sales burn it too.
	function burnBalance() external onlyRoot {
		_burnAll();
	}

	/// The new rate counts from now; accrual at the old rate is dropped.
	function setRate(uint256 ratePerDay_) external onlyRoot {
		if (ratePerDay_ > MAX_VALUE) revert InvalidValue();
		ratePerDay = ratePerDay_;
		lastTrickle = block.timestamp;
		remainder = 0;
		emit RateSet(ratePerDay_);
	}

	function setMinPrice(uint256 minPrice_) external onlyRoot {
		if (minPrice_ > MAX_VALUE) revert InvalidValue();
		minPrice = minPrice_;
		emit MinPriceSet(minPrice_);
	}

	function setCap(uint256 capBps_) external onlyRoot {
		if (capBps_ == 0 || capBps_ > BPS) revert InvalidCap();
		capBps = capBps_;
		emit CapSet(capBps_);
	}

	function pause() public virtual onlyRoot {
		paused = true;
		emit Paused();
	}

	/// Accrual starts from now; nothing accrues while paused.
	function resume() external onlyRoot {
		paused = false;
		lastTrickle = block.timestamp;
		remainder = 0;
		try dex.getReserves(dotAsset, jamkbAsset) returns (uint256, uint256 r) {
			refReserve = r;
		} catch {
			refReserve = 0;
		}
		emit Resumed();
	}

	/// Sends the whole JAMKB budget to `to`, the custody account's address.
	function returnBudget(address to) external onlyRoot {
		uint256 amount = jamkb.balanceOf(address(this));
		if (!jamkb.transfer(to, amount)) revert TransferFailed();
		emit BudgetReturned(to, amount);
	}

	/// Burns the contract's whole spendable balance. `address(this).balance` is the balance
	/// above the existential deposit and holds, in the 18-decimal unit of native value in
	/// revive; the assumed `burn` takes the same unit. Reverts if anything is left.
	function _burnAll() private {
		uint256 value = address(this).balance;
		if (value == 0) return;
		try system.burn(value) {}
		catch {
			revert BurnFailed();
		}
		if (address(this).balance != 0) revert BurnFailed();
		emit Burned(value);
	}

	function _skip(Skip reason) private returns (uint256, uint256) {
		emit Skipped(reason);
		return (0, 0);
	}

	function _no(Plan memory p, Skip reason) private pure returns (Plan memory) {
		p.reason = reason;
		return p;
	}

	function _ceilDiv(uint256 a, uint256 b) private pure returns (uint256) {
		return a == 0 ? 0 : (a - 1) / b + 1;
	}
}
