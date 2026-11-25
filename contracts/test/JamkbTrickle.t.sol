// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {JamkbTrickle} from "../src/JamkbTrickle.sol";
import {IAssetConversion} from "../src/interfaces/IAssetConversion.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";
import {ISystem} from "../src/interfaces/ISystem.sol";
import {MockDex, MockSystem, MockToken, credit} from "./Mocks.sol";

/// The Foundry cheatcodes used here, declared locally so the tests need no forge-std.
interface Vm {
	function warp(uint256) external;
}

contract JamkbTrickleTest {
	Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

	bytes constant DOT = hex"0100";
	// (0, [PalletInstance(50), GeneralIndex(21000000)]), 21000000 as SCALE compact.
	bytes constant JAMKB = hex"000204320502bd0105";

	// JAMKB has 3 decimals, DOT 10.
	uint256 constant SEED_DOT = 50_000e10;
	uint256 constant SEED_JAMKB = 500_000e3;
	uint256 constant BUDGET = 200_000e3;
	uint256 constant RATE = 3_000e3; // 125 JAMKB an hour
	uint256 constant HOURLY = RATE / 24;
	// 0.08 DOT per JAMKB = 8e5 planks per base unit.
	uint256 constant FLOOR = 8e5 * 1e18;
	uint256 constant CAP_BPS = 30; // 0.3% of the pool's JAMKB, 12 hours of accrual

	MockSystem sys;
	MockToken token;
	MockDex dex;
	JamkbTrickle t;

	function deploy(uint256 minPrice) internal returns (JamkbTrickle x) {
		x = new JamkbTrickle(
			IAssetConversion(address(dex)), ISystem(address(sys)), IERC20(address(token)), DOT,
			JAMKB, RATE, minPrice, CAP_BPS
		);
		token.mint(address(x), BUDGET);
	}

	function setUp() public {
		vm.warp(1_000_000);
		sys = new MockSystem();
		token = new MockToken();
		dex = new MockDex(token, DOT, SEED_DOT, SEED_JAMKB);
		token.setDex(address(dex));
		t = deploy(FLOOR);
		root(true);
		t.resume();
		root(false);
	}

	function root(bool r) internal {
		sys.setRoot(r);
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

	function reason() internal view returns (JamkbTrickle.Skip) {
		return t.preview().reason;
	}

	// Selling and burning.

	function test_StartsPaused() public {
		JamkbTrickle fresh = deploy(FLOOR);
		vm.warp(block.timestamp + 1 days);
		check(fresh.preview().reason == JamkbTrickle.Skip.Paused, "paused");
		(uint256 sold,) = fresh.trickle();
		eq(sold, 0, "a new contract does not sell before Root resumes it");
	}

	function test_HourlySaleBurnsTheWholeBalance() public {
		vm.warp(block.timestamp + 1 hours);
		uint256 quote = dex.amountOut(HOURLY, SEED_JAMKB, SEED_DOT);
		JamkbTrickle.Plan memory p = t.preview();
		check(p.ok && p.amount == HOURLY && p.quote == quote, "preview matches the sale");
		(uint256 sold, uint256 got) = t.trickle();
		eq(sold, HOURLY, "sold one hour of accrual");
		eq(got, quote, "received the quoted DOT");
		eq(sys.burned(), quote, "burned what it received");
		eq(address(t).balance, 0, "no DOT left in the contract");
		eq(token.balanceOf(address(t)), BUDGET - HOURLY, "budget reduced by the sale");
	}

	function test_StrayDotIsBurnedWithTheNextSale() public {
		credit(address(t), 7e10);
		vm.warp(block.timestamp + 1 hours);
		(, uint256 got) = t.trickle();
		eq(sys.burned(), got + 7e10, "the sale burns the stray DOT too");
		eq(address(t).balance, 0, "nothing left");
	}

	function test_RootBurnsStrayDotWithoutASale() public {
		credit(address(t), 7e10);
		(bool ok, bytes memory ret) = address(t).call(abi.encodeCall(JamkbTrickle.burnBalance, ()));
		check(!ok && bytes4(ret) == JamkbTrickle.NotRoot.selector, "burnBalance needs Root");
		root(true);
		t.burnBalance();
		eq(sys.burned(), 7e10, "burned");
		eq(address(t).balance, 0, "nothing left");
	}

	function test_BurnFailureRevertsTheSale() public {
		sys.setBurnFails(true);
		vm.warp(block.timestamp + 1 hours);
		try t.trickle() {
			revert("a failed burn must revert the whole sale");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickle.BurnFailed.selector, "BurnFailed");
		}
		eq(token.balanceOf(address(t)), BUDGET, "budget untouched");
	}

	function test_ABurnThatLeavesDotRevertsTheSale() public {
		sys.setBurnShort(1);
		vm.warp(block.timestamp + 1 hours);
		try t.trickle() {
			revert("a partial burn must revert");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickle.BurnFailed.selector, "BurnFailed");
		}
	}

	function test_ZeroMinPriceStillSells() public {
		JamkbTrickle x = deploy(0);
		root(true);
		x.resume();
		root(false);
		vm.warp(block.timestamp + 1 hours);
		eq(x.preview().minOut, 1, "the least accepted is one plank, as the pallet requires");
		(uint256 sold,) = x.trickle();
		eq(sold, HOURLY, "sells with no floor");
	}

	// Accrual.

	function test_SecondCallInSameBlockSellsNothing() public {
		vm.warp(block.timestamp + 1 hours);
		t.trickle();
		uint256 burnedBefore = sys.burned();
		check(reason() == JamkbTrickle.Skip.NothingAccrued, "nothing accrued");
		(uint256 sold,) = t.trickle();
		eq(sold, 0, "second call sells nothing");
		eq(sys.burned(), burnedBefore, "second call burns nothing");
	}

	function test_ManyCallersSellTheSameAsOne() public {
		uint256 total;
		for (uint256 i = 0; i < 600; i++) {
			vm.warp(block.timestamp + 6);
			(uint256 sold,) = t.trickle();
			total += sold;
		}
		eq(total, HOURLY, "frequent calls sell exactly the hourly amount");
	}

	function test_TinyRateIsNotRoundedAwayByFrequentCalls() public {
		root(true);
		t.setRate(24); // 1 base unit an hour
		root(false);
		uint256 total;
		for (uint256 i = 0; i < 600; i++) {
			vm.warp(block.timestamp + 6);
			(uint256 sold,) = t.trickle();
			total += sold;
		}
		eq(total, 1, "the accrual survives calls that round to zero");
	}

	function test_LongGapIsCappedAndExcessDropped() public {
		vm.warp(block.timestamp + 30 days);
		(uint256 sold,) = t.trickle();
		eq(sold, SEED_JAMKB * CAP_BPS / 10_000, "a long gap sells at most the cap");
		vm.warp(block.timestamp + 1 hours);
		(sold,) = t.trickle();
		eq(sold, HOURLY, "the excess above the cap is not carried over");
	}

	function test_FrontRunDoesNotRaiseTheCap() public {
		token.mint(address(0xBAD), SEED_JAMKB / 10);
		vm.warp(block.timestamp + 30 days);
		dex.sell(address(0xBAD), SEED_JAMKB / 10);
		(uint256 sold,) = t.trickle();
		eq(sold, SEED_JAMKB * CAP_BPS / 10_000, "cap from the reserve before the front-run");
	}

	function test_PoolBelowTheRecordedReserveUsesTheCurrentOne() public {
		dex.buy(address(0xB0B), 10_000e10); // takes JAMKB out of the pool
		vm.warp(block.timestamp + 30 days);
		eq(t.preview().cap, dex.rJam() * CAP_BPS / 10_000, "the smaller, current reserve");
	}

	function test_SetRateDropsTheRemainder() public {
		vm.warp(block.timestamp + 1 hours);
		root(true);
		t.setRate(RATE * 2);
		root(false);
		eq(t.remainder(), 0, "remainder dropped");
		vm.warp(block.timestamp + 1 hours);
		(uint256 sold,) = t.trickle();
		eq(sold, HOURLY * 2, "the new rate from the change, nothing from before it");
	}

	// Skips.

	function test_BelowMinPriceSkipsAndKeepsAccrual() public {
		root(true);
		t.setMinPrice(2e6 * 1e18); // 0.2 DOT, above the pool's 0.1
		root(false);
		vm.warp(block.timestamp + 1 hours);
		uint256 last = t.lastTrickle();
		check(reason() == JamkbTrickle.Skip.BelowMinPrice, "below the minimum price");
		(uint256 sold,) = t.trickle();
		eq(sold, 0, "no sale below the minimum price");
		eq(t.lastTrickle(), last, "accrual kept");

		dex.buy(address(0xB0B), 30_000e10);
		vm.warp(block.timestamp + 1 hours);
		(sold,) = t.trickle();
		eq(sold, 2 * HOURLY, "the kept accrual sells once the price is back");
	}

	function test_BudgetRunsOut() public {
		root(true);
		t.returnBudget(address(0xC0DE));
		root(false);
		token.mint(address(t), 100e3);
		vm.warp(block.timestamp + 1 hours);
		(uint256 sold,) = t.trickle();
		eq(sold, 100e3, "sells only what is left");
		vm.warp(block.timestamp + 1 hours);
		check(reason() == JamkbTrickle.Skip.NoBudget, "no budget");
		(sold,) = t.trickle();
		eq(sold, 0, "nothing to sell after the budget is gone");
	}

	function test_NoPoolSkips() public {
		dex.setExists(false);
		vm.warp(block.timestamp + 1 hours);
		check(reason() == JamkbTrickle.Skip.NoPool, "no pool");
		(uint256 sold,) = t.trickle();
		eq(sold, 0, "no sale without a pool, and no revert");
	}

	function test_TinyPoolSkipsAsCapZero() public {
		MockDex tiny = new MockDex(token, DOT, 300, 300);
		JamkbTrickle x = new JamkbTrickle(
			IAssetConversion(address(tiny)), ISystem(address(sys)), IERC20(address(token)), DOT,
			JAMKB, RATE, 0, CAP_BPS
		);
		token.mint(address(x), BUDGET);
		root(true);
		x.resume();
		root(false);
		vm.warp(block.timestamp + 1 hours);
		check(x.preview().reason == JamkbTrickle.Skip.CapZero, "cap rounds to zero");
	}

	function test_QuoteFailureSkips() public {
		dex.setQuoteFails(true);
		vm.warp(block.timestamp + 1 hours);
		check(reason() == JamkbTrickle.Skip.QuoteFailed, "quote failed");
		(uint256 sold,) = t.trickle();
		eq(sold, 0, "a failed quote is a skip, not a revert");
	}

	function test_SwapFailureSkipsAndLeavesNoTrace() public {
		dex.setSwapFails(true);
		vm.warp(block.timestamp + 1 hours);
		uint256 last = t.lastTrickle();
		(uint256 sold,) = t.trickle();
		eq(sold, 0, "a failed swap is a skip, not a revert");
		eq(t.lastTrickle(), last, "state untouched");
		dex.setSwapFails(false);
		(sold,) = t.trickle();
		eq(sold, HOURLY, "the accrual sells once the swap works");
	}

	// Control.

	function test_PauseStopsAndResumeDoesNotCatchUp() public {
		root(true);
		t.pause();
		root(false);
		vm.warp(block.timestamp + 1 days);
		(uint256 sold,) = t.trickle();
		eq(sold, 0, "paused");
		root(true);
		t.resume();
		root(false);
		vm.warp(block.timestamp + 1 hours);
		(sold,) = t.trickle();
		eq(sold, HOURLY, "the paused day does not accrue");
	}

	function test_ReturnBudget() public {
		root(true);
		t.returnBudget(address(0xC0DE));
		eq(token.balanceOf(address(0xC0DE)), BUDGET, "budget returned to custody");
		eq(token.balanceOf(address(t)), 0, "nothing left");
	}

	function test_SettersRejectBadValues() public {
		root(true);
		try t.setCap(0) {
			revert("cap 0 accepted");
		} catch {}
		try t.setCap(10_001) {
			revert("cap above 100% accepted");
		} catch {}
		uint256 big = uint256(type(uint128).max) + 1;
		try t.setRate(big) {
			revert("an overflowing rate accepted");
		} catch {}
		try t.setMinPrice(big) {
			revert("an overflowing minimum price accepted");
		} catch {}
	}

	function test_ConstructorRejectsAddressesWithoutCode() public {
		try new JamkbTrickle(
			IAssetConversion(address(dex)), ISystem(address(0x0900)), IERC20(address(token)), DOT,
			JAMKB, RATE, FLOOR, CAP_BPS
		) {
			revert("a system address without code accepted");
		} catch (bytes memory r) {
			check(bytes4(r) == JamkbTrickle.NoCode.selector, "NoCode");
		}
	}

	function test_ConfigHashCoversTheParameters() public {
		bytes32 h = t.configHash();
		root(true);
		t.setCap(25);
		check(t.configHash() != h, "the hash changes with the cap");
	}

	function test_AdminCallsNeedRoot() public {
		bytes[8] memory calls = [
			abi.encodeCall(JamkbTrickle.setRate, (1)),
			abi.encodeCall(JamkbTrickle.setMinPrice, (1)),
			abi.encodeCall(JamkbTrickle.setCap, (1)),
			abi.encodeCall(JamkbTrickle.pause, ()),
			abi.encodeCall(JamkbTrickle.resume, ()),
			abi.encodeCall(JamkbTrickle.returnBudget, (address(this))),
			abi.encodeCall(JamkbTrickle.burnBalance, ()),
			abi.encodeCall(JamkbTrickle.setRate, (0))
		];
		for (uint256 i = 0; i < calls.length; i++) {
			(bool ok, bytes memory ret) = address(t).call(calls[i]);
			if (ok || bytes4(ret) != JamkbTrickle.NotRoot.selector) revert("admin call without Root");
		}
	}
}
