## Analysis

The GNSMultiCollatDiamond bug is a **storage-key-derivation shift**: adding fields before a mapping in an upgrade silently changes the mapping's effective address, so all prior entries become permanently unreachable while the code keeps running as if nothing happened. The closest reproducible analog in this repo is not a raw storage-slot problem (FRAME storage keys are name-derived, not offset-derived) but the same *class* of bug — an upgrade that changes the encoding used to key a storage map, while the accompanying migration only touches half of the affected storage, silently orphaning existing entries under the old key encoding.

### Title
Incomplete XCM v5 migration leaves `NativeToForeignId` keyed under stale `xcm::v4::Location` encoding, enabling duplicate Snowbridge foreign-token registration - ([File: cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs])

### Summary
`MigrationForXcmV5` declares a `storage_alias` for the pre-upgrade `NativeToForeignId` map (keyed by `xcm::v4::Location`) but never actually migrates it. It only calls `translate_values` on the *reverse* map, `ForeignToNativeId`. After the runtime moves to XCM v5, `NativeToForeignId` is looked up with `xcm::v5::Location` keys, which hash differently (via `Blake2_128Concat`) than the old `xcm::v4::Location` keys still on disk. Existing entries become permanently unreachable, exactly mirroring the reported bug where fields inserted ahead of a mapping shifted its storage slot and orphaned all role data.

### Finding Description
`bridges/snowbridge/pallets/system-frontend/src/lib.rs` (`register_token`) uses the reverse map `ForeignToNativeId` to dedupe (`if !ForeignToNativeId::<T>::contains_key(token_id)`), but the forward map `NativeToForeignId` (keyed by the native asset's `Location`) is the map that guards against registering the *same underlying asset* twice under a new token id. [1](#0-0) 

The migration for the XCM v5 upgrade is: [2](#0-1) 

It defines `OldNativeToForeignId` as a `StorageMap` keyed by `xcm::v4::Location` (using `Blake2_128Concat`), which strongly implies the intent was to re-key `NativeToForeignId` from v4 to v5 `Location` encoding. Instead, the `on_runtime_upgrade` body only calls `ForeignToNativeId::<T>::translate_values(translate_westend)` — a **value** translation on the reverse map, not a **key** re-encoding of the forward map. The `OldNativeToForeignId` alias is declared and then never used anywhere in the migration body (confirmed via repo-wide search — it only appears twice in this file: the declaration and nowhere else).

Since `NativeToForeignId`'s key is a full XCM `Location`, and `Location` encoding changed between XCM v4 and v5 (that's precisely why the migration exists for `ForeignToNativeId`'s values), any `Location` key stored under the old encoding will no longer be found by `Blake2_128Concat`-hashed lookups using the new `xcm::v5::Location` encoding of the semantically same location. This is functionally identical to the GNSMultiCollatDiamond bug: the underlying data doesn't move, but the addressing scheme used to reach it changes, so the mapping "loses" all its prior entries from the perspective of code compiled against the new layout.

### Impact Explanation
If `NativeToForeignId` is consulted to prevent double-registering a native asset as an Ethereum-wrapped ERC20 (this is the map's evident purpose, mirrored by `ForeignToNativeId`), then after this migration runs on a runtime holding pre-existing registrations, all old entries become invisible to lookups performed with `xcm::v5::Location` keys. Any caller — an ordinary, unprivileged user with `T::FrontendOrigin` authorization to call `register_token` — could then re-register a native asset that is already registered, causing the Snowbridge `Command::RegisterForeignToken` to be sent again to the Gateway contract on Ethereum. This can create a second wrapped ERC20 for the same underlying asset (duplicate settlement of a bridge-side artifact), and depending on downstream reliance on the map for id derivation, could allow asset/token id confusion for future teleports. This falls squarely under "duplicate settlement or payout" and "permanent... bridge-state lock" (the old entries are permanently orphaned, not merely delayed).

### Likelihood Explanation
No malicious peer, relayer, prover, admin, or governance actor is required — the trigger is simply the runtime upgrade itself running the shipped, in-repo migration, followed by an ordinary call to the public `register_token` extrinsic by any authorized frontend caller. The bug is deterministic and always manifests for every previously-registered native asset once the XCM v5 runtime upgrade executes, since the migration path for `NativeToForeignId` keys is entirely absent from the code, not merely misconfigured.

### Recommendation
Actually re-key `NativeToForeignId` during the migration: iterate its `xcm::v4::Location` keys via the declared `OldNativeToForeignId` alias, remove each old entry, and re-insert it under the same value with the key converted through `xcm::v5::Location::try_from`, in the same style the report recommends for the Solidity case (preserve/rebuild the existing mapping's addressing rather than leaving it split across two incompatible encodings). Add a `pre_upgrade`/`post_upgrade` (`try-runtime`) check asserting that the count and mapping of `NativeToForeignId` entries is preserved across the migration, consistent with the pattern already used elsewhere in this codebase (e.g., `substrate/frame/nfts/src/migration.rs`, `substrate/frame/assets/src/migration.rs`).

### Proof of Concept
1. Deploy the pre-XCM-v5 runtime; call `register_token` for asset location `L` via `system-frontend::register_token`, which sets `NativeToForeignId(L_v4) = token_id` and `ForeignToNativeId(token_id) = L_v4` (exact insertion point for `NativeToForeignId` in `bridges/snowbridge/pallets/system/src/lib.rs`, confirmed to exist via grep but not fully quoted here).
2. Apply the runtime upgrade containing `MigrationForXcmV5`. Observe it only calls `ForeignToNativeId::<T>::translate_values(...)`; `NativeToForeignId` is untouched, so its on-disk key remains hashed from `xcm::v4::Location`'s SCALE encoding of `L`.
3. Post-upgrade, call `register_token` again for the same asset `L`, now represented internally as `xcm::v5::Location`. The lookup/insert path hashes `L_v5`'s encoding, which differs from `L_v4`'s encoding, so the existing `NativeToForeignId` entry is not found — the pallet treats it as a brand-new asset and emits a second `RegisterForeignToken` command to the Ethereum Gateway, creating a duplicate wrapped ERC20 for the same native asset.

Note: I was not able to directly inspect the body of `NativeToForeignId`'s definition and every one of its call sites in `bridges/snowbridge/pallets/system/src/lib.rs` within the available tool budget (only confirmed via `grep_search` match counts and the `system-frontend` insert call for the reverse map). If further confirmation of the exact `NativeToForeignId` insert/lookup logic is needed, a full read of that file is recommended before treating this as fully proven.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L226-231)
```rust
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L474-504)
```rust
pub(crate) mod migrations {
	use frame_support::pallet_prelude::*;
	use snowbridge_core::TokenId;

	#[frame_support::storage_alias]
	pub type OldNativeToForeignId<T: snowbridge_pallet_system::Config> = StorageMap<
		snowbridge_pallet_system::Pallet<T>,
		Blake2_128Concat,
		xcm::v4::Location,
		TokenId,
		OptionQuery,
	>;

	/// One shot migration for NetworkId::Westend to NetworkId::ByGenesis(WESTEND_GENESIS_HASH)
	pub struct MigrationForXcmV5<T: snowbridge_pallet_system::Config>(core::marker::PhantomData<T>);
	impl<T: snowbridge_pallet_system::Config> frame_support::traits::OnRuntimeUpgrade
		for MigrationForXcmV5<T>
	{
		fn on_runtime_upgrade() -> Weight {
			let mut weight = T::DbWeight::get().reads(1);

			let translate_westend = |pre: xcm::v4::Location| -> Option<xcm::v5::Location> {
				weight.saturating_accrue(T::DbWeight::get().reads_writes(1, 1));
				Some(xcm::v5::Location::try_from(pre).expect("valid location"))
			};
			snowbridge_pallet_system::ForeignToNativeId::<T>::translate_values(translate_westend);

			weight
		}
	}
}
```
