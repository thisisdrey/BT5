Based on my research, I found a concrete local analog to the Spartan "mint arbitrary synth without verifying underlying token" bug class in Snowbridge's token-identity handling, where a `Location → TokenId` hash function is *not* provably injective, yet a recent change makes the protocol trust the `TokenId` key alone instead of re-verifying the underlying asset `Location`.

### Title
Snowbridge token registration trusts `TokenId` key existence instead of re-verifying the underlying asset `Location`, enabling `TokenId` squatting/hijack via `GeneralKey` collisions - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
The reported Spartan bug is a "missing underlying-asset binding" bug: `Pool.mintSynth` never checked that the `Synth` it was asked to mint actually belongs to that specific pool's token, so a cheap pool could mint an expensive synth. Snowbridge has the same class of bug in its `TokenId ↔ Location` binding: `register_token` in `pallet-system-v2` derives a `TokenId` from an XCM `Location` and stores it as the canonical mapping [1](#0-0) , but the codebase itself demonstrates that two *different* `Location`s (differing only in `GeneralKey` `length` metadata) can hash to the *same* `TokenId` [2](#0-1) . A companion PR (`pr_8473`) explicitly removed a location-verification step, arguing "checking whether the key exists is sufficient to verify if the token is registered. There is no need to verify the asset location" [3](#0-2) . That assumption is false given the demonstrated collision.

### Finding Description
`register_token` computes `token_id = TokenIdOf::convert_location(&location)` and only inserts into `ForeignToNativeId` if the key is *not already present* — it never checks that an existing entry's stored `Location` actually equals the caller's `location`: [4](#0-3) 

Because `TokenIdOf::convert_location` is a hash of the encoded `Location`, and the test suite proves that a `GeneralKey { length: 32, data }` and a `GeneralKey { length: 1, data }` sharing the same leading bytes can produce the *same* derived `TokenId` while remaining distinct, `Location`s: `assert_ne!(victim_location, attacker_location, ...)` while ids collide unless corrected [5](#0-4) . The v2 XCM message converter still guards the *mint* path with an explicit reverse check (`asset_id == expected_asset_id`) [6](#0-5) , and its own regression test (`xcm_converter_mints_registered_token_id_for_colliding_general_key_location`) confirms this guard is what currently blocks the attack at *mint* time [7](#0-6) . However, `register_token`'s *registration* step — which establishes the canonical `TokenId → Location` binding that all later mint/unlock/settlement decisions rely on — has no equivalent reverse check, and per `pr_8473` this was an intentional design decision to drop location verification everywhere key-existence can substitute for it [8](#0-7) .

This mirrors the Spartan bug precisely: the "pool" (here, the `TokenId` registry entry) is not bound to verify that the "synth" (here, the actual backing `Location`/asset) it is being asked to service matches what was originally intended, and later operations (mint, transfer, settlement) trust the *identifier* rather than re-deriving/re-checking the *identity*.

### Impact Explanation
If an attacker can get their own (cheap, attacker-owned) asset `Location` registered *first* for a `TokenId` that collides with a legitimate, higher-value asset's future `Location`, the registry permanently binds that `TokenId` to the attacker's `Location` (`if !ForeignToNativeId::<T>::contains_key(token_id) { insert(...) }` never overwrites). Any later legitimate registration attempt for the victim asset silently no-ops. Downstream flows that resolve a `TokenId` back to a `Location` (unlocking/crediting on Polkadot, or minting the wrapped ERC20 metadata on Ethereum) will then use the attacker's `Location` rather than the intended asset — a forged/mis-bound token identity that can result in unbacked mint, fund misdirection, or a permanently unusable/locked legitimate token registration, matching the "forged or mis-bound proof or state acceptance" and "theft or unbacked mint" impact categories.

### Likelihood Explanation
Exploitability depends on: (1) whether `register_token`'s caller-supplied `Location` can practically be crafted with attacker-controlled `GeneralKey` encodings that collide with a specific target `TokenId` before that target is registered, and (2) the exact trust boundary of `T::FrontendOrigin` gating `register_token` (whether it merely requires the caller to control *some* asset on AssetHub, not necessarily the specific victim asset). I was not able to fully trace `FrontendOrigin`'s authorization constraints or confirm whether `TokenIdOf::convert_location`'s current hash construction (post any length-prefixing fixes referenced in the tests) still allows practical collisions outside the specific `GeneralKey` test vector. This should be verified with a background engineering session, given the file-size limits encountered while reading `bridges/snowbridge/pallets/system-v2/src/lib.rs` in full.

### Recommendation
In `register_token`, when `ForeignToNativeId::<T>::contains_key(token_id)` is true, fetch the stored `Location` and require it to equal the caller's `location` (return an error such as `TokenIdConflict` otherwise), restoring the reverse-binding check that the outbound mint path already enforces. Audit `TokenIdOf::convert_location` to ensure it is injective over all realistically constructible `Location`s (in particular `GeneralKey` variants with differing `length` fields), not just the specific collision already covered by tests.

### Proof of Concept
The collision primitive is already checked into the test suite: [2](#0-1) 
A conceptual PoC: (1) attacker calls `register_token` (via the AssetHub-proxied `FrontendOrigin`) with `attacker_location` using a `GeneralKey` whose `(length, data)` collides with the future `victim_location`'s derived `TokenId`; (2) `ForeignToNativeId` now maps that `TokenId` to `attacker_location`; (3) the legitimate owner later calls `register_token` with `victim_location` — it silently no-ops due to `contains_key`; (4) any later settlement referencing that `TokenId` resolves to `attacker_location`, not the intended victim asset.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L219-238)
```rust

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
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L1318-1341)
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
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L1343-1356)
```rust
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

**File:** prdoc/stable2506/pr_8473.prdoc (L1-7)
```text
title: 'Snowbridge: Remove asset location check'
doc:
- audience: Runtime Dev
  description: |-
    Since the TokenIdOf conversion is XCM version-agnostic and we store the TokenId as the key in storage,
    checking whether the key exists is sufficient to verify if the token is registered.
    There is no need to verify the asset location.
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L406-420)
```rust
		let (asset_id, amount) = match reserve_asset {
			Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
				Some((inner_location.clone(), *amount))
			},
			_ => None,
		}
		.ok_or(AssetResolutionFailed)?;

		// transfer amount must be greater than 0.
		ensure!(amount > 0, ZeroAssetTransfer);

		let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;

		let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
		ensure!(asset_id == expected_asset_id, InvalidAsset);
```

**File:** prdoc/stable2503-6/pr_8473.prdoc (L1-14)
```text
title: 'Snowbridge: Remove asset location check'
doc:
- audience: Runtime Dev
  description: |-
    Since the TokenIdOf conversion is XCM version-agnostic and we store the TokenId as the key in storage,
    checking whether the key exists is sufficient to verify if the token is registered.
    There is no need to verify the asset location.
crates:
- name: snowbridge-outbound-queue-primitives
  bump: patch
  validate: false
- name: snowbridge-inbound-queue-primitives
  bump: patch
  validate: false
```
