"""Account and location encodings, computed offline so the referenda can name them in advance."""

import hashlib
from decimal import Decimal

DOT_DECIMALS = 10
JAMKB_DECIMALS = 3
ASSETS_PALLET_INDEX = 50  # pallet-assets (trust-backed) on Asset Hub Polkadot
ASSET_CONVERSION_PALLET_ID = b"py/ascon"


def blake2_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def compact(n: int) -> bytes:
    """SCALE compact encoding."""
    if n < 1 << 6:
        return bytes([n << 2])
    if n < 1 << 14:
        return ((n << 2) | 1).to_bytes(2, "little")
    if n < 1 << 30:
        return ((n << 2) | 2).to_bytes(4, "little")
    raw = n.to_bytes((n.bit_length() + 7) // 8, "little")
    return bytes([((len(raw) - 4) << 2) | 3]) + raw


def planks(amount: str, decimals: int) -> int:
    """'1.5' with 10 decimals -> 15_000_000_000. Rejects amounts finer than one plank."""
    value = Decimal(amount) * (Decimal(10) ** decimals)
    if value != value.to_integral_value():
        raise ValueError(f"{amount} has more than {decimals} decimals")
    return int(value)


def dot_location() -> dict:
    return {"parents": 1, "interior": "Here"}


def jamkb_location(asset_id: int) -> dict:
    return {
        "parents": 0,
        "interior": {"X2": [{"PalletInstance": ASSETS_PALLET_INDEX}, {"GeneralIndex": asset_id}]},
    }


def _encode_dot_location() -> bytes:
    # parents 1, Junctions::Here (0)
    return bytes([1, 0])


def _encode_jamkb_location(asset_id: int) -> bytes:
    # parents 0, Junctions::X2 (2), Junction::PalletInstance (4) u8, Junction::GeneralIndex (5) compact
    return bytes([0, 2, 4, ASSETS_PALLET_INDEX, 5]) + compact(asset_id)


def pool_account_id(asset_id: int) -> bytes:
    """Asset Hub Polkadot pool account: blake2_256(("py/ascon", (DOT, JAMKB)).encode()).

    `AccountIdConverter<AssetConversionPalletId, PoolId>`; the native asset comes first
    (`WithFirstAsset`). Checked against the `PoolCreated` event in the simulation.
    """
    pool_id = _encode_dot_location() + _encode_jamkb_location(asset_id)
    return blake2_256(ASSET_CONVERSION_PALLET_ID + pool_id)


def keyless_account_id(seed: str, sub: str = "") -> bytes:
    """`PalletId(seed).into_sub_account_truncating(sub)`: "modl" ++ seed ++ sub, zero padded.

    Nobody holds a key for it. Only Root reaches it, with `utility.dispatch_as`.
    """
    seed_bytes = seed.encode()
    if len(seed_bytes) != 8:
        raise ValueError("account_seed must be 8 bytes, like a PalletId")
    raw = b"modl" + seed_bytes + sub.encode()
    if len(raw) > 32:
        raise ValueError("sub-account name too long")
    return raw.ljust(32, b"\0")


def treasury_account_id() -> bytes:
    return keyless_account_id("py/trsry")


def task_id(name: str) -> bytes:
    """The scheduler task name: blake2_256 of a readable label."""
    return blake2_256(name.encode())
