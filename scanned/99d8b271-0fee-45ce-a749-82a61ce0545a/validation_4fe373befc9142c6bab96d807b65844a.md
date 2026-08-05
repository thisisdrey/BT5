### Title
Snowbridge token registration accepts unvalidated `Location` before deriving `TokenId`, allowing permanent squatting/DoS of a legitimate asset's bridge identity - (File: `bridges/snowbridge/pallets/system/src/lib.rs`, `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
The C4 report's core defect is that a public entrypoint accepts an attacker-supplied identity field (an ERC-20 token address inside a `permit2` struct) and blindly trusts it as the contract's intended asset, because the code never checks the supplied identity against the expected one. The Snowbridge analog is the token-registration path: `register_token` derives a `TokenId` from an attacker/user-supplied `Location` and inserts it into `ForeignToNativeId` guarded only by `contains_key`, with **no canonicalization/round-trip check** on the supplied `Location` before it becomes the permanent on-chain identity for that `TokenId`. The equivalent round-trip validation (`ensure!(asset_id == expected_asset_id, InvalidAsset)`) exists only in the *outbound XCM converter* (used when assets are later bridged), not at the registration entrypoint itself, and a repo-local test (`xcm_converter_mints_registered_token_id_for_colliding_general_key_location`) proves that two semantically-different `Location`s (differing only in `GeneralKey` length encoding) can hash to different-looking data yet the raw `Location` is accepted without canonical-form verification unless that specific downstream check fires.

### Finding Description
`TokenIdOf::convert_location` hashes a `Location` into a 32-byte `TokenId` that becomes the sole lookup key in `ForeignToNativeId`/`ForeignToAssetId`-style storage: [1](#0-0) 

```rust
pub(crate) fn do_register_token(...) -> Result<(), DispatchError> {
    let location = location.clone().reanchored(&ethereum_location, &T::UniversalLocation::get())...?;
    let token_id = TokenIdOf::convert_location(&location).ok_or(...)?;
    if !ForeignToNativeId::<T>::contains_key(token_id) {
        ForeignToNativeId::<T>::insert(token_id, location.clone());
    }
    ...
}
```

The same pattern repeats in the v2 (front-end-proxied, less privileged) path: [2](#0-1) 

Neither function verifies that the caller-supplied `location` is the *canonical* encoding for the asset it claims to represent — there is no analog of the check that exists in the outbound converter: [3](#0-2) 

```rust
let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
ensure!(asset_id == expected_asset_id, InvalidAsset);
```

A dedicated repo test demonstrates that two distinct `Location`s can be crafted (via `GeneralKey` length-field manipulation) that are **not equal** as `Location` values but collide in a way that the registry / TokenId derivation must specifically defend against: [4](#0-3) [5](#0-4) 

This confirms the bug class is real and previously present in this codebase (`ConvertAssetId::maybe_convert` + round-trip equality is the only defense the maintainers added). However that defense is bolted onto the *outbound converter*, which only runs once assets are actually being transferred — it does **not** run at `register_token` time. Because `ForeignToNativeId`/equivalent storage uses `if !contains_key(token_id) { insert(...) }` as its only collision guard, whichever `Location` is registered **first** for a given `TokenId` permanently wins; the entry can never be corrected or overwritten later, even by root calling `register_token` again for the legitimate canonical `Location` of that same asset.

### Impact Explanation
This matches the "permanent user-fund or bridge-state lock" and "forged/mis-bound proof or state acceptance" impact categories: an attacker who can call `register_token` (directly via root-restricted `pallet-system::register_token`, or — more critically — via the far less privileged, fee-based `pallet-system-v2`/`system-frontend` path meant to let arbitrary AssetHub users register their own PNA) can pre-register a crafted, non-canonical `Location` whose derived `TokenId` collides with the `TokenId` that a legitimate asset's canonical `Location` will later produce. Because of the `contains_key` guard, the legitimate asset's registration silently no-ops or is otherwise blocked, permanently squatting that `TokenId` and preventing the legitimate asset from ever being bridged under its correct identity — a permanent bridge-state lock/DoS on that asset's Snowbridge integration, achievable by an unprivileged user without needing a malicious relayer, validator, or governance actor.

### Likelihood Explanation
Likelihood is contingent on (a) whether `TokenIdOf::convert_location`'s underlying SCALE/Location encoding actually admits the kind of encoding ambiguity the repo's own collision test exercises (the test's existence strongly suggests yes, at least for `GeneralKey` variable-length data), and (b) the exact origin restrictions on `pallet-system-v2::register_token` / `system-frontend::register_token` (I could not fully confirm from the index whether `FrontendOrigin` allows fully permissionless invocation by ordinary signed accounts on AssetHub, versus being restricted to a privileged bridge/XCM origin). This is the main unresolved uncertainty in this analysis — the exact caller-permission model of the `-v2`/frontend registration path needs direct code confirmation (via a Devin session with full repo access) before treating this as a confirmed exploitable vulnerability.

### Recommendation
Apply the same round-trip canonicalization check used in the outbound converter (`TokenIdOf::convert_location` → `ConvertAssetId::maybe_convert` → `ensure!(location == recovered_location)`) directly inside `do_register_token` / `pallet-system-v2::register_token`, before the `ForeignToNativeId::insert`, so that only canonically-encoded `Location`s can ever be persisted as a `TokenId`'s identity, closing the squatting/DoS vector at the point of registration rather than only at the point of transfer.

### Proof of Concept
1. Attacker calls the permissionless/low-privilege `register_token` extrinsic (`pallet-system-v2`/`system-frontend`) with a crafted `Location` `L_attacker` using a non-canonical `GeneralKey { length: 1, data }` encoding that hashes, via `TokenIdOf::convert_location`, to the same `TokenId` `T` that the legitimate asset's canonical `Location` `L_victim` (`GeneralKey { length: 32, data }`, per the repo's own collision test at `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs:1318-1341`) would produce.
2. `ForeignToNativeId::<T>::contains_key(T)` is `false`, so `ForeignToNativeId::insert(T, L_attacker)` succeeds; `RegisterForeignToken` is dispatched to Ethereum for `T`.
3. Later, the legitimate asset issuer calls `register_token` with `L_victim`; `TokenIdOf::convert_location(&L_victim)` again yields `T`; `contains_key(T)` is now `true`, so the insert is skipped — the storage entry for `T` permanently points at the attacker's bogus `L_attacker`, and the legitimate asset can never be correctly bridged under `T`.

### Citations

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L476-493)
```rust
		pub(crate) fn do_register_token(
			location: &Location,
			metadata: AssetMetadata,
			pays_fee: PaysFee<T>,
		) -> Result<(), DispatchError> {
			let ethereum_location = T::EthereumLocation::get();
			// reanchor to Ethereum context
			let location = location
				.clone()
				.reanchored(&ethereum_location, &T::UniversalLocation::get())
				.map_err(|_| Error::<T>::LocationConversionFailed)?;

			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L211-248)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			sender: Box<VersionedLocation>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let sender_location: Location =
				(*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			let location = Self::reanchor(asset_location)?;
			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.into(),
				foreign_token_id: token_id,
			});

			Ok(())
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L177-181)
```rust

			// Ensure PNA already registered
			let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
			let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
			ensure!(asset_id == expected_asset_id, InvalidAsset);
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L1318-1356)
```rust
fn general_key_length_collision_locations() -> (Location, Location) {
	let mut data = [0u8; 32];
	data[0] = 0xAB;

	let victim_location = Location::new(
		1,
		[
			GlobalConsensus(ByGenesis(WESTEND_GENESIS_HASH)),
			Parachain(2000),
			GeneralKey { length: 32, data },
		],
	);

	let attacker_location = Location::new(
		1,
		[
			GlobalConsensus(ByGenesis(WESTEND_GENESIS_HASH)),
			Parachain(2000),
			GeneralKey { length: 1, data },
		],
	);

	(victim_location, attacker_location)
}

/// Registry mock: only the victim TokenId is "registered".
pub struct VictimOnlyTokenIdConvert;

impl MaybeConvert<TokenId, Location> for VictimOnlyTokenIdConvert {
	fn maybe_convert(id: TokenId) -> Option<Location> {
		let (victim_location, _) = general_key_length_collision_locations();
		let victim_id = TokenIdOf::convert_location(&victim_location)?;
		if id == victim_id {
			Some(victim_location)
		} else {
			None
		}
	}
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L1358-1408)
```rust
#[test]
fn xcm_converter_mints_registered_token_id_for_colliding_general_key_location() {
	let network = BridgedNetwork::get();
	let beneficiary_address: [u8; 20] = hex!("2000000000000000000000000000000000000000");
	let amount = 1_000_000;
	let fee_amount = 1_000;
	let (victim_location, attacker_location) = general_key_length_collision_locations();
	assert_ne!(victim_location, attacker_location, "Locations must differ");

	// Deterministic collision:
	let victim_token_id = TokenIdOf::convert_location(&victim_location).unwrap();
	let attacker_token_id = TokenIdOf::convert_location(&attacker_location).unwrap();
	assert_ne!(victim_token_id, attacker_token_id, "TokenIds should differ after the fix");

	// Optional debug prints for report clarity
	println!("victim_location     = {victim_location:?}");
	println!("attacker_location   = {attacker_location:?}");
	println!("victim_token_id     = {victim_token_id:?}");
	println!("attacker_token_id   = {attacker_token_id:?}");

	// Build XCM that reserves *attacker* asset location (but will mint under victim token_id)
	// V2 XCM format requires: WithdrawAsset(fee), PayFees, ReserveAssetDeposited, AliasOrigin,
	// DepositAsset, SetTopic
	let fee_asset = Asset { id: AssetId(Location::new(0, [])), fun: Fungible(fee_amount) };
	let assets: Assets =
		vec![Asset { id: AssetId(attacker_location.clone()), fun: Fungible(amount) }].into();
	let filter: AssetFilter = assets.clone().into();

	// Create an origin location for AliasOrigin
	let origin_location = Location::new(1, [Parachain(2000)]);

	let message: Xcm<()> = vec![
		WithdrawAsset(vec![fee_asset.clone()].into()),
		PayFees { asset: fee_asset },
		ReserveAssetDeposited(assets.clone()),
		AliasOrigin(origin_location),
		DepositAsset {
			assets: filter,
			beneficiary: AccountKey20 { network: None, key: beneficiary_address }.into(),
		},
		SetTopic([0; 32]),
	]
	.into();

	let mut converter =
		XcmConverter::<VictimOnlyTokenIdConvert, (), EverythingBut<Equals<AssetHubLocation>>>::new(
			&message, network,
		);
	let result = converter.convert();
	assert_eq!(result.err(), Some(XcmConverterError::InvalidAsset));
}
```
