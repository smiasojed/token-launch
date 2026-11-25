// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

/// The system precompile of pallet-revive (`ISystem.sol` in `substrate/frame/revive/uapi/sol`).
interface ISystem {
	/// True when the caller of the contract calling this function is Root. Exists today.
	function callerIsRoot() external view returns (bool);

	/// ASSUMED, does not exist yet: burns `value` of the calling contract's balance and lowers
	/// the total issuance. `value` is in the unit of `address(this).balance`, the 18-decimal
	/// native value of revive, like every other native value in its interfaces.
	function burn(uint256 value) external;
}
