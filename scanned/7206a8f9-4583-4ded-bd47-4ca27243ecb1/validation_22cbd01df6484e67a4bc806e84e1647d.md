Based on the evidence gathered, I found a concrete local analog in `pallet-assets-precompiles`.

### Title
Precompile `name()`/`symbol()` for pallet-assets permanently revert for legitimately-registered assets with non-UTF-8 metadata - ([File: substrate/frame/assets/precompiles/src/lib.rs])

### Summary
The external report's core broken invariant is: code assumes an optional/loosely-specified external data field will always conform to a specific type (`string`), and hard-fails when a legitimately created token doesn't match that assumption. The same pattern exists in `substrate/frame/assets/precompiles/src/lib.rs`: the `IERC20Metadata` precompile implementation assumes `pallet_assets` asset `name`/`symbol` metadata is always valid UTF-8, but `pallet-assets` itself never enforces that invariant when the metadata is set.

### Finding Description
`pallet_assets::Pallet::do_set_metadata` only validates the *length* of `name`/`symbol` against `T::StringLimit` via `BoundedVec` conversion (`Error::<T, I>::BadMetadata` on overflow) — it performs no UTF-8 validation: [1](#0-0) 
The `AssetMetadata` type itself stores `name`/`symbol` as raw `BoundedString` byte vectors with no encoding constraint: [2](#0-1) 

The asset-hub ERC20 precompile, added specifically "to provide full ERC20 compatibility... essential for proper EVM wallet and tooling integration" per PR #10971, assumes the opposite — that stored bytes are always valid UTF-8 — and unconditionally converts and reverts otherwise: [3](#0-2) [4](#0-3) 

Because the asset owner (a legitimate, non-privileged party with ownership of that specific asset id) can call `set_metadata`/`force_set_metadata` with arbitrary non-UTF-8 bytes and this succeeds and is stored on-chain without any guard preventing it: [5](#0-4) 

any subsequent call through the EVM-facing precompile's `name()` or `symbol()` for that asset will always revert with `"Invalid UTF-8 in name"` / `"Invalid UTF-8 in symbol"`, exactly mirroring the reported bug class where a legitimately structured token cannot be handled by code that assumes a specific optional-field format.

### Impact Explanation
This breaks the intended guarantee that `pallet-assets`-backed tokens fully implement `IERC20Metadata` for EVM tooling/wallets/DeFi integrations built on `pallet-revive`'s precompile layer, as explicitly stated to be the purpose of the precompile in PR #10971. Any asset owner (even unintentionally, e.g. by passing raw bytes instead of a UTF-8 string to `set_metadata`) permanently and irrecoverably (from the EVM side) breaks ERC20 `name()`/`symbol()` compatibility for that asset — a runtime bug that compromises intended behavior of a public entrypoint (the precompile), not caused by a malicious/privileged actor abusing governance, but by ordinary use of a documented, permitted asset-owner call.

### Likelihood Explanation
High likelihood of accidental triggering: `set_metadata`/`force_set_metadata` accept arbitrary `Vec<u8>` with no UTF-8 check, so any asset owner supplying binary/non-ASCII bytes (e.g., copy-paste errors, non-English encodings, or intentionally embedding non-UTF-8 ticker bytes as some real-world tokens do) will trip this. No privileged or malicious actor is required — the asset's own legitimate owner performing a normal metadata update is enough.

### Recommendation
Either (a) enforce UTF-8 validity for `name`/`symbol` at `do_set_metadata`/`force_set_metadata` time in `substrate/frame/assets/src/functions.rs` and `substrate/frame/assets/src/lib.rs` so invalid metadata can never be stored, or (b) make the precompile's `name()`/`symbol()` in `substrate/frame/assets/precompiles/src/lib.rs` degrade gracefully (e.g., `String::from_utf8_lossy` or a fixed placeholder) instead of reverting, consistent with how the external report recommended handling non-conforming `symbol()` return types.

### Proof of Concept
1. Create an asset via `pallet_assets::create` and become its owner.
2. Call `set_metadata(id, name: vec![0xFF, 0xFE], symbol: vec![0xFF, 0xFE], decimals: 8)` — this succeeds because `do_set_metadata` only checks length via `BoundedVec` conversion, at [6](#0-5) .
3. From an EVM context (or another contract), call the precompile's `IERC20::symbolCall` for that asset id.
4. Execution hits `String::from_utf8(metadata.symbol.to_vec()).map_err(...)` at [7](#0-6)  and reverts with `"Invalid UTF-8 in symbol"` for every future call, for as long as the metadata remains unchanged.

Note: I was not able to verify from the index whether any downstream contract/wallet integration test explicitly exercises this failure path, since precompile integration tests were not fully covered by my searches; a Devin session with full repository access could confirm by running `substrate/frame/assets/precompiles` tests with non-UTF-8 metadata fixtures.

### Citations

**File:** substrate/frame/assets/src/functions.rs (L1058-1070)
```rust
	/// Do set metadata
	pub(super) fn do_set_metadata(
		id: T::AssetId,
		from: &T::AccountId,
		name: Vec<u8>,
		symbol: Vec<u8>,
		decimals: u8,
	) -> DispatchResult {
		let bounded_name: BoundedVec<u8, T::StringLimit> =
			name.clone().try_into().map_err(|_| Error::<T, I>::BadMetadata)?;
		let bounded_symbol: BoundedVec<u8, T::StringLimit> =
			symbol.clone().try_into().map_err(|_| Error::<T, I>::BadMetadata)?;

```

**File:** substrate/frame/assets/src/types.rs (L190-204)
```rust
#[derive(Clone, Encode, Decode, Eq, PartialEq, Default, Debug, MaxEncodedLen, TypeInfo)]
pub struct AssetMetadata<DepositBalance, BoundedString> {
	/// The balance deposited for this metadata.
	///
	/// This pays for the data stored in this struct.
	pub deposit: DepositBalance,
	/// The user friendly name of this asset. Limited in length by `StringLimit`.
	pub name: BoundedString,
	/// The ticker symbol for this asset. Limited in length by `StringLimit`.
	pub symbol: BoundedString,
	/// The number of decimals this asset uses to represent one unit.
	pub decimals: u8,
	/// Whether the asset metadata may be changed by a non Force origin.
	pub is_frozen: bool,
}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L663-693)
```rust
	/// Execute the name call.
	fn name(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		env.charge(<Runtime as Config<Instance>>::WeightInfo::get_metadata())?;

		let metadata = pallet_assets::Pallet::<Runtime, Instance>::get_metadata(asset_id)
			.ok_or(Error::Revert(Revert { reason: "Metadata not found".into() }))?;

		let name = alloc::string::String::from_utf8(metadata.name.to_vec())
			.map_err(|_| Error::Revert(Revert { reason: "Invalid UTF-8 in name".into() }))?;

		Ok(IERC20::nameCall::abi_encode_returns(&name))
	}

	/// Execute the symbol call.
	fn symbol(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		env.charge(<Runtime as Config<Instance>>::WeightInfo::get_metadata())?;

		let metadata = pallet_assets::Pallet::<Runtime, Instance>::get_metadata(asset_id)
			.ok_or(Error::Revert(Revert { reason: "Metadata not found".into() }))?;

		let symbol = alloc::string::String::from_utf8(metadata.symbol.to_vec())
			.map_err(|_| Error::Revert(Revert { reason: "Invalid UTF-8 in symbol".into() }))?;

		Ok(IERC20::symbolCall::abi_encode_returns(&symbol))
	}
```

**File:** prdoc/stable2603/pr_10971.prdoc (L1-19)
```text
title: "Implement IERC20Metadata for pallet-assets precompiles"

doc:
  - audience: Runtime Dev
    description: |
      Implements the missing ERC20 metadata functions (`name`, `symbol`, `decimals`) for the
      pallet-assets precompile to provide full ERC20 compatibility. These functions are essential
      for proper EVM wallet and tooling integration.

      The precompile implementation reads metadata from pallet-assets storage and returns properly
      formatted values with appropriate gas charging using dedicated weight functions. All functions
      include proper error handling for missing metadata and invalid UTF-8 encoding.

      Benchmarks have been added to measure the weight of metadata reads, and corresponding weight
      functions have been implemented in the WeightInfo trait.

      The IERC20.sol interface file has been reorganized to clearly separate and document methods
      from the base IERC20 interface and the IERC20Metadata extension, with links to the original
      OpenZeppelin contracts for better maintainability.
```

**File:** substrate/frame/assets/src/lib.rs (L1396-1424)
```rust
		/// Set the metadata for an asset.
		///
		/// Origin must be Signed and the sender should be the Owner of the asset `id`.
		///
		/// Funds of sender are reserved according to the formula:
		/// `MetadataDepositBase + MetadataDepositPerByte * (name.len + symbol.len)` taking into
		/// account any already reserved funds.
		///
		/// - `id`: The identifier of the asset to update.
		/// - `name`: The user friendly name of this asset. Limited in length by `StringLimit`.
		/// - `symbol`: The exchange symbol for this asset. Limited in length by `StringLimit`.
		/// - `decimals`: The number of decimals this asset uses to represent one unit.
		///
		/// Emits `MetadataSet`.
		///
		/// Weight: `O(1)`
		#[pallet::call_index(17)]
		#[pallet::weight(T::WeightInfo::set_metadata(name.len() as u32, symbol.len() as u32))]
		pub fn set_metadata(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			name: Vec<u8>,
			symbol: Vec<u8>,
			decimals: u8,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let id: T::AssetId = id.into();
			Self::do_set_metadata(id, &origin, name, symbol, decimals)
		}
```
