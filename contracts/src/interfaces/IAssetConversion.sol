// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

/// The asset conversion precompile of pallet-revive
/// (`substrate/frame/asset-conversion/precompiles`). Only the functions the trickle uses.
/// Assets are SCALE-encoded `Location`s. Amounts are in the assets' base units (DOT in planks).
interface IAssetConversion {
	function swapExactTokensForTokens(
		bytes[] calldata path,
		uint256 amountIn,
		uint256 amountOutMin,
		address sendTo,
		bool keepAlive
	) external returns (uint256 amountOut);

	function quoteExactTokensForTokens(
		bytes calldata asset1,
		bytes calldata asset2,
		uint256 amount,
		bool includeFee
	) external view returns (uint256);

	function getReserves(bytes calldata asset1, bytes calldata asset2)
		external
		view
		returns (uint256 reserve1, uint256 reserve2);
}
