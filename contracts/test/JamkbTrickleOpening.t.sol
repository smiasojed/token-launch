// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {JamkbTrickle} from "../src/JamkbTrickle.sol";
import {JamkbTrickleOpening} from "../src/JamkbTrickleOpening.sol";
import {IAssetConversion} from "../src/interfaces/IAssetConversion.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";
import {ISystem} from "../src/interfaces/ISystem.sol";
import {MockDex, MockSystem, MockToken} from "./Mocks.sol";

interface Vm {
	function warp(uint256) external;
}

contract JamkbTrickleOpeningTest {
	Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

	bytes constant DOT = hex"0100";
	bytes constant JAMKB = hex"000204320502bd0105";

	uint256 constant SEED_DOT = 50_000e10;
	uint256 constant SEED_JAMKB = 500_000e3; // price 0.1 DOT = 1e6 planks per base unit
	uint256 constant SEED_PRICE = 1e6;
	uint256 constant BUDGET = 200_000e3;
	uint256 constant RATE = 3_000e3;
	uint256 constant HOURLY = RATE / 24;
	uint256 constant HARD_MIN = 2e5 * 1e18; // 0.02 DOT
	uint256 constant CAP_BPS = 30;
	address constant BUYER = address(0xB0B);
	address constant ATTACKER = address(0xBAD);

	MockSystem sys;
	MockToken token;
	MockDex dex;
	JamkbTrickleOpening t;

	function config() internal pure returns (JamkbTrickleOpening.OpeningConfig memory) {
		return JamkbTrickleOpening.OpeningConfig({
			window: 3 days,
			thresholdBps: 9_000,
			boostBpsPerDay: 480, // 4.8% of the pool a day, 8x the base rate at this seed
			confirmGap: 1 days,
			extraBudget: 100_000e3,
			maxStarts: 3
		});
	}

	function create(JamkbTrickleOpening.OpeningConfig memory c)
		internal
		returns (JamkbTrickleOpening x)
	{
		x = new JamkbTrickleOpening(
			IAssetConversion(address(dex)), ISystem(address(sys)), IERC20(address(token)), DOT,
			JAMKB, RATE, HARD_MIN, CAP_BPS, c
		);
		token.mint(address(x), BUDGET);
	}

	function deploy(JamkbTrickleOpening.OpeningConfig memory c, uint256 duration)
		internal
		returns (JamkbTrickleOpening x)
	{
		x = create(c);
		sys.setRoot(true);
		x.resume();
		x.startOpening(duration);
		sys.setRoot(false);
	}

	function setUp() public {
		vm.warp(1_000_000);
		sys = new MockSystem();
		token = new MockToken();
		dex = new MockDex(token, DOT, SEED_DOT, SEED_JAMKB);
		token.setDex(address(dex));
		t = deploy(config(), 30 days);
	}

	function eq(uint256 a, uint256 b, string memory what) internal pure {
		if (a != b) revert(string.concat(what, ": ", str(a), " != ", str(b)));
	}

	function check(bool ok, string memory what) internal pure {
		if (!ok) revert(what);
	}

	function str(uint256 v) internal pure returns (string memory s) {
		if (v == 0) return "0";
		while (v > 0) {
			s = string.concat(string(abi.encodePacked(bytes1(uint8(48 + v % 10)))), s);
			v /= 10;
		}
	}

	function abs(int256 v) internal pure returns (uint256) {
		return uint256(v < 0 ? -v : v);
	}

	function hour() internal returns (uint256 sold, uint256 got) {
		vm.warp(block.timestamp + 1 hours);
		return t.trickle();
	}

	function hours_(uint256 n) internal {
		for (uint256 i = 0; i < n; i++) hour();
	}

	function sqrt(uint256 x) internal pure returns (uint256 y) {
		if (x == 0) return 0;
		y = x;
		uint256 z = (x + 1) / 2;
		while (z < y) {
			y = z;
			z = (x / z + z) / 2;
		}
	}

	/// Buyers take any JAMKB below `market` (planks per base unit): they buy until the pool
	/// price is back at `market`, never over it.
	function marketBuys(uint256 market) internal {
		uint256 targetDot = sqrt(dex.rDot() * dex.rJam() * market);
		if (targetDot > dex.rDot()) dex.buy(BUYER, targetDot - dex.rDot());
	}

	function boosting() internal view returns (bool) {
		return t.phase() == JamkbTrickleOpening.Phase.Boosting;
	}

	function off() internal view returns (bool) {
		return t.phase() == JamkbTrickleOpening.Phase.Off;
	}

	/// Runs hourly sales with buyers at `market` until the opening is off or `limit` hours pass.
	function runMarket(uint256 market, uint256 limit) internal returns (uint256 h) {
		for (h = 1; h <= limit && !off(); h++) {
			hour();
			marketBuys(market);
		}
	}

	// The signal.

	function test_NetBuysFromThePriceMatchTheJamkbBought() public {
		hour();
		uint256 bought = dex.buy(BUYER, 1_000e10);
		hour();
		uint256 got = uint256(t.windowBuys());
		// Buys carry a 0.3% fee into the pool, which the formula does not see.
		check(got * 1_000 >= bought * 995 && got * 1_000 <= bought * 1_005,
			string.concat("net buys ", str(got), " vs bought ", str(bought)));
	}

	function test_BuysBeforeTheFirstSaleCount() public {
		uint256 bought = dex.buy(BUYER, 1_000e10);
		hour();
		check(uint256(t.windowBuys()) * 1_000 >= bought * 995, "the baseline is taken at the start");
	}

	function test_LiquidityChangesAreNotBuys() public {
		hour();
		dex.addLiquidity(10_000e10);
		hour();
		check(abs(t.windowBuys()) <= 10, "a liquidity add is not a buy or a sell");
	}

	function test_BuyAndSellBackNetsOut() public {
		hour();
		uint256 bought = dex.buy(BUYER, 2_000e10);
		dex.sell(BUYER, bought);
		hour();
		check(abs(t.windowBuys()) * 100 <= bought, "a round trip nets out to under 1%");
	}

	// The phases.

	function test_CorrectSeedEndsWithoutABoost() public {
		for (uint256 h = 1; h <= 100 && !off(); h++) {
			(uint256 sold,) = hour();
			eq(sold, HOURLY, "base rate while measuring");
			marketBuys(SEED_PRICE);
		}
		check(off(), "buyers took the release, the opening ended without a boost");
		eq(t.extraSold(), 0, "no extra sold");
	}

	function test_HigherMarketEndsWithoutABoost() public {
		runMarket(12e5, 100);
		check(off(), "the opening ended");
		eq(t.extraSold(), 0, "no extra sold");
	}

	function test_NoBuyersBoostsAfterMeasuringAndConfirming() public {
		hours_(95);
		check(!boosting(), "no boost before no demand is confirmed a day after the window");
		hours_(1);
		check(boosting(), "the boost started");
		(uint256 sold,) = hour();
		check(sold > HOURLY * 7, "about 8x the base rate");
		for (uint256 h = 0; h < 300 && t.extraSold() < 100_000e3; h++) hour();
		eq(t.extraSold(), 100_000e3, "extra sold reaches the budget exactly");
		(sold,) = hour();
		eq(sold, HOURLY, "the base rate once the extra budget is used");
	}

	/// Seeded at 0.1 DOT, market at 0.08 DOT. Lowering a constant-product price by 20% takes
	/// selling 1/sqrt(0.8) - 1 = 11.8% of the pool's JAMKB, about 59,000 here.
	function test_FindsALowerMarketPrice() public {
		uint256 market = 8e5;
		uint256 foundAt;
		uint256 endedAt;
		for (uint256 h = 1; h <= 30 * 24 && endedAt == 0; h++) {
			hour();
			marketBuys(market);
			if (foundAt == 0 && (dex.rDot() / dex.rJam()) * 1_000 <= market * 1_010) foundAt = h;
			if (off()) endedAt = h;
		}
		check(foundAt > 0 && foundAt <= 8 * 24, string.concat("found at hour ", str(foundAt)));
		check(endedAt > foundAt && endedAt <= foundAt + 3 * 24,
			string.concat("ended at hour ", str(endedAt), ", found at ", str(foundAt)));
		check(t.extraSold() < 100_000e3, "ended by demand, not by the extra budget");
	}

	function test_NoSaleWhileMeasuringStillCountsTheBuys() public {
		// A buyer takes five times the release, and no sale runs for the whole window.
		dex.buy(BUYER, 4_500e10);
		vm.warp(block.timestamp + 72 hours);
		for (uint256 h = 1; h <= 30 && !off(); h++) {
			check(!boosting(), "never boosts");
			hour();
		}
		check(off(), "the buys were counted and the opening ended");
		eq(t.extraSold(), 0, "no extra sold");
	}

	function test_ALongGapIsScaledToTheWindow() public {
		hours_(97);
		check(boosting(), "boosting");
		// 8 days without a sale; buyers take 40% of the daily release each day.
		for (uint256 d = 0; d < 8; d++) {
			vm.warp(block.timestamp + 1 days);
			dex.buy(BUYER, 120e10);
		}
		hours_(30);
		check(boosting(), "8 days of buys do not land in the window at full weight");
	}

	// Attacks.

	function test_FlashBuyAtTheMeasuringEndDoesNotEndTheOpening() public {
		hours_(71);
		vm.warp(block.timestamp + 1 hours);
		uint256 bought = dex.buy(ATTACKER, 900e10);
		t.trickle();
		dex.sell(ATTACKER, bought);
		hours_(30);
		check(boosting(), "the flash buy did not end the opening; no demand led to the boost");
	}

	/// A buy and a sell-back around one sale, as one transaction.
	function flashBuyAround(uint256 dotIn) internal {
		vm.warp(block.timestamp + 1 hours);
		uint256 bought = dex.buy(ATTACKER, dotIn);
		t.trickle();
		dex.sell(ATTACKER, bought);
	}

	function test_OneFlashTradeDoesNotSetASignal() public {
		hours_(97);
		check(boosting(), "boosting");
		flashBuyAround(2_000e10);
		check(t.pending() == JamkbTrickleOpening.Signal.None, "no signal from one trade");
		hour();
		check(t.pending() == JamkbTrickleOpening.Signal.None, "nor after the sell-back");
	}

	function test_FlashBuysADayApartDoNotEndTheOpening() public {
		hours_(71);
		flashBuyAround(2_000e10);
		hours_(23);
		flashBuyAround(2_000e10);
		hours_(10);
		check(boosting(), "two flash buys a day apart did not confirm demand");
	}

	function test_FlashSellAtTheMeasuringEndDoesNotStartTheBoost() public {
		token.mint(ATTACKER, 12_000e3);
		for (uint256 h = 1; h <= 100 && !off(); h++) {
			vm.warp(block.timestamp + 1 hours);
			if (h == 72) dex.sell(ATTACKER, 12_000e3);
			t.trickle();
			marketBuys(SEED_PRICE);
		}
		check(off(), "the opening ended");
		eq(t.extraSold(), 0, "the flash sell did not start a boost");
	}

	function test_FlashSellsDuringConfirmationDoNotResetIt() public {
		uint256 market = 8e5;
		token.mint(ATTACKER, 100_000_000e3);
		uint256 h;
		for (h = 1; h <= 30 * 24 && !off(); h++) {
			vm.warp(block.timestamp + 1 hours);
			if (t.pending() == JamkbTrickleOpening.Signal.Demand && h % 6 == 0) {
				dex.sell(ATTACKER, uint256(t.windowBuys()) + 5_000e3);
			}
			t.trickle();
			marketBuys(market);
		}
		check(off(), "the opening ended");
		check(h < 12 * 24, string.concat("ended in time, at hour ", str(h)));
		check(t.extraSold() < 100_000e3, "the extra budget was not drained");
	}

	function test_PerBlockCallsWithBuyBacksDoNotEndTheBoost() public {
		hours_(97);
		check(boosting(), "boosting");
		for (uint256 i = 0; i < 300; i++) {
			vm.warp(block.timestamp + 6);
			(, uint256 got) = t.trickle();
			dex.buy(BUYER, got * 1_004 / 1_000);
		}
		check(boosting(), "still boosting");
	}

	function test_ALargeBuySoldBackDoesNotEndTheBoost() public {
		hours_(97);
		uint256 bought = dex.buy(BUYER, 2_000e10);
		hour();
		dex.sell(BUYER, bought);
		hours_(30);
		check(boosting(), "a buy sold back within the day does not end the boost");
	}

	function test_ALargeBuyHeldForADayEndsTheBoost() public {
		hours_(97);
		dex.buy(BUYER, 2_000e10);
		hours_(25);
		check(boosting(), "not confirmed yet");
		hours_(2);
		check(off(), "demand at two sales a day apart ends the boost");
	}

	// Control.

	function test_DeadlineReturnsToTheBaseRate() public {
		t = deploy(config(), 5 days);
		hours_(100);
		check(boosting(), "boosting");
		hours_(20);
		check(off(), "the deadline ended the opening");
		(uint256 sold,) = hour();
		eq(sold, HOURLY, "the base rate after the deadline");
	}

	function test_RootCanEndTheOpeningAndEndingTwiceIsANoOp() public {
		hours_(97);
		sys.setRoot(true);
		t.endOpening();
		t.endOpening();
		sys.setRoot(false);
		check(off(), "ended");
		(uint256 sold,) = hour();
		eq(sold, HOURLY, "the base rate after Root ends the opening");
	}

	function test_PauseEndsTheOpening() public {
		hours_(97);
		sys.setRoot(true);
		t.pause();
		check(off(), "pausing ended the opening");
		t.resume();
		t.startOpening(30 days);
		check(t.phase() == JamkbTrickleOpening.Phase.Measuring, "a new opening measures again");
	}

	function test_StartOpeningChecks() public {
		sys.setRoot(true);
		try t.startOpening(30 days) {
			revert("started while one runs");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickleOpening.OpeningRunning.selector, "OpeningRunning");
		}
		t.endOpening();
		t.pause();
		try t.startOpening(30 days) {
			revert("started while paused");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickleOpening.InvalidOpening.selector, "paused");
		}
		t.resume();
		dex.setExists(false);
		try t.startOpening(30 days) {
			revert("started without a pool");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickleOpening.InvalidOpening.selector, "no pool");
		}
		dex.setExists(true);
		try t.startOpening(4 days) {
			revert("started shorter than measuring and confirming");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickleOpening.InvalidOpening.selector, "too short");
		}
	}

	function test_StartsAreBoundedAndNeedRoot() public {
		(bool ok, bytes memory ret) =
			address(t).call(abi.encodeCall(JamkbTrickleOpening.startOpening, (30 days)));
		check(!ok && bytes4(ret) == JamkbTrickle.NotRoot.selector, "start needs Root");
		(ok, ret) = address(t).call(abi.encodeCall(JamkbTrickleOpening.endOpening, ()));
		check(!ok && bytes4(ret) == JamkbTrickle.NotRoot.selector, "end needs Root");
		(ok, ret) = address(t).call(abi.encodeCall(JamkbTrickle.pause, ()));
		check(!ok && bytes4(ret) == JamkbTrickle.NotRoot.selector, "pause needs Root");

		sys.setRoot(true);
		t.endOpening();
		t.startOpening(30 days);
		t.endOpening();
		t.startOpening(30 days);
		t.endOpening();
		try t.startOpening(30 days) {
			revert("a fourth start accepted");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickleOpening.NoStartsLeft.selector, "NoStartsLeft");
		}
	}

	function test_BadConfigRejected() public {
		JamkbTrickleOpening.OpeningConfig[4] memory bad;
		for (uint256 i = 0; i < 4; i++) bad[i] = config();
		bad[0].confirmGap = 0;
		bad[1].confirmGap = bad[1].window;
		bad[2].thresholdBps = 10_001;
		bad[3].boostBpsPerDay = 0;
		for (uint256 i = 0; i < 4; i++) {
			try new JamkbTrickleOpening(
				IAssetConversion(address(dex)), ISystem(address(sys)), IERC20(address(token)), DOT,
				JAMKB, RATE, HARD_MIN, CAP_BPS, bad[i]
			) {
				revert(string.concat("bad config ", str(i), " accepted"));
			} catch {}
		}
	}

	function test_ConfigHashCoversTheOpening() public {
		JamkbTrickleOpening.OpeningConfig memory c = config();
		c.thresholdBps = 8_000;
		check(create(c).configHash() != create(config()).configHash(), "differs by config");
	}
}
