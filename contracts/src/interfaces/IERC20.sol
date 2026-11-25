// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

/// The ERC20 precompile of pallet-assets, one address per asset. Only the functions the trickle
/// uses.
interface IERC20 {
	function balanceOf(address account) external view returns (uint256);
	function transfer(address to, uint256 value) external returns (bool);
}
