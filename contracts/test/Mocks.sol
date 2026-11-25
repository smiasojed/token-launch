// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

/// Stand-ins for the precompiles. DOT is native balance, set with Foundry's `deal`, because a
/// precompile credits a contract without running its code. One plank is one unit of native
/// balance here; on a chain the native value has 18 decimals.

interface VmDeal {
	function deal(address, uint256) external;
}

VmDeal constant VM = VmDeal(address(uint160(uint256(keccak256("hevm cheat code")))));

function credit(address to, uint256 amount) {
	VM.deal(to, to.balance + amount);
}

contract MockSystem {
	bool public root;
	bool public burnFails;
	/// The burn leaves this much behind, to test the check after it.
	uint256 public burnShort;
	uint256 public burned;

	function setRoot(bool r) external {
		root = r;
	}

	function setBurnFails(bool f) external {
		burnFails = f;
	}

	function setBurnShort(uint256 s) external {
		burnShort = s;
	}

	function callerIsRoot() external view returns (bool) {
		return root;
	}

	function burn(uint256 value) external {
		require(!burnFails, "burn fails");
		require(msg.sender.balance >= value, "burn exceeds balance");
		uint256 done = value > burnShort ? value - burnShort : 0;
		VM.deal(msg.sender, msg.sender.balance - done);
		burned += done;
	}
}

contract MockToken {
	mapping(address => uint256) public balanceOf;
	address public dex;

	function setDex(address d) external {
		dex = d;
	}

	function mint(address to, uint256 amount) external {
		balanceOf[to] += amount;
	}

	function transfer(address to, uint256 value) external returns (bool) {
		require(balanceOf[msg.sender] >= value, "balance");
		balanceOf[msg.sender] -= value;
		balanceOf[to] += value;
		return true;
	}

	/// The DEX precompile moves assets without an allowance.
	function move(address from, address to, uint256 value) external {
		require(msg.sender == dex, "only dex");
		require(balanceOf[from] >= value, "balance");
		balanceOf[from] -= value;
		balanceOf[to] += value;
	}
}

/// Constant-product pool with a 0.3% fee, as pallet-asset-conversion.
contract MockDex {
	MockToken public immutable token;
	bytes32 public immutable dotKey;
	bool public exists = true;
	bool public quoteFails;
	bool public swapFails;
	uint256 public rDot;
	uint256 public rJam;

	constructor(MockToken t, bytes memory dotAsset, uint256 d, uint256 j) {
		token = t;
		dotKey = keccak256(dotAsset);
		rDot = d;
		rJam = j;
	}

	function setExists(bool e) external {
		exists = e;
	}

	function setQuoteFails(bool f) external {
		quoteFails = f;
	}

	function setSwapFails(bool f) external {
		swapFails = f;
	}

	function amountOut(uint256 amountIn, uint256 rIn, uint256 rOut) public pure returns (uint256) {
		uint256 inWithFee = amountIn * 997;
		return inWithFee * rOut / (rIn * 1000 + inWithFee);
	}

	function getReserves(bytes calldata a1, bytes calldata) external view returns (uint256, uint256) {
		require(exists, "PoolNotFound");
		return keccak256(a1) == dotKey ? (rDot, rJam) : (rJam, rDot);
	}

	function quoteExactTokensForTokens(bytes calldata a1, bytes calldata, uint256 amount, bool)
		external
		view
		returns (uint256)
	{
		require(exists && !quoteFails, "quote fails");
		require(keccak256(a1) != dotKey, "mock sells JAMKB only");
		return amountOut(amount, rJam, rDot);
	}

	function swapExactTokensForTokens(
		bytes[] calldata path,
		uint256 amountIn,
		uint256 amountOutMin,
		address sendTo,
		bool
	) external returns (uint256 out) {
		require(exists && !swapFails, "swap fails");
		require(path.length == 2 && keccak256(path[1]) == dotKey, "mock sells JAMKB only");
		// As the pallet: a zero minimum is rejected.
		require(amountIn > 0 && amountOutMin > 0, "ZeroAmount");
		out = amountOut(amountIn, rJam, rDot);
		require(out >= amountOutMin, "ProvidedMinimumNotSufficientForSwap");
		token.move(msg.sender, address(this), amountIn);
		rJam += amountIn;
		rDot -= out;
		credit(sendTo, out);
	}

	/// Liquidity added at the pool's price.
	function addLiquidity(uint256 dotIn) external {
		rJam += dotIn * rJam / rDot;
		rDot += dotIn;
	}

	/// A holder swaps JAMKB for DOT.
	function sell(address from, uint256 jamIn) external returns (uint256 out) {
		out = amountOut(jamIn, rJam, rDot);
		token.move(from, address(this), jamIn);
		rJam += jamIn;
		rDot -= out;
		credit(from, out);
	}

	/// A buyer swaps DOT for JAMKB.
	function buy(address to, uint256 dotIn) external returns (uint256 out) {
		out = amountOut(dotIn, rDot, rJam);
		rDot += dotIn;
		rJam -= out;
		token.mint(to, out);
	}
}
