### Title
Missing round-trip verification of `token_id → Location` for foreign (PNA) assets in the Snowbridge inbound-queue v2 converter allows resolution of the wrong reserve asset — ([File: bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs])

### Summary
The external report's core broken invariant is: *a system accepts a token reference supplied without verifying it is actually bound to the value it is supposed to represent, so a victim's accounting-token action resolves against the wrong underlying asset.* The Polkadot-SDK analog is in Snowbridge's Ethereum→Polkadot (V2) inbound message converter, where a `ForeignTokenERC20` asset's `token_id` is converted to a `Location` via a single one-way lookup with no reverse-binding check, unlike the equivalent outbound (Polkadot→Ethereum) path, which explicitly re-derives and compares the two directions.

### Finding Description
`TokenIdOf::convert_location` derives a `TokenId` (hash) from a `Location`, and `ForeignToNativeId`/`ConvertAssetId` provide the reverse mapping back to a `Location`. Both directions must agree for the identifier to safely stand in for "the asset that was actually registered/reserved."

In the outbound v2 converter (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`, `extract_polkadot_native_assets`, lines 178-181), the code performs a **round-trip check**:
```
let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
ensure!(asset_id == expected_asset_id, InvalidAsset);
```
This exists specifically to defeat location-encoding collisions — proven by the dedicated regression test `xcm_converter_mints_registered_token_id_for_colliding_general_key_location` in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs` (lines 1358-1408), which constructs two *different* locations (`victim_location`, `attacker_location`) that previously hashed to colliding `TokenId`s and asserts the fix now rejects the mismatch (`InvalidAsset`).

The inbound v2 converter (`bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`, `prepare`, lines 181-198) handles the analogous case for `EthereumAsset::ForeignTokenERC20` but performs only the **one-way** lookup:
```rust
EthereumAsset::ForeignTokenERC20 { token_id, value } => {
    let asset_location = ConvertAssetId::maybe_convert(*token_id)
        .ok_or(ConvertMessageError::InvalidAsset)?;
    ...
    let asset: Asset = (reanchored_asset_location, *value).into();
    assets.push(AssetTransfer::ReserveWithdraw(asset));
},
```
There is no equivalent `TokenIdOf::convert_location(&asset_location) == *token_id` re-derivation/comparison. `token_id` here is a value carried in the relayed Ethereum message payload (an `H256`/`TokenId` chosen by whatever emitted the Gateway event), so the only thing standing between "attacker-influenced identifier" and "reserve-withdraw of an arbitrary registered asset location" is the one-way correctness of the hash function embedded in `TokenIdOf`/`ConvertAssetId`. Because the outbound side needed a dedicated fix and test to close exactly this class of collision, the same underlying hashing/lookup primitive is reused on the inbound side without the mitigating check.

### Impact Explanation
If any two distinct `Location`s can be crafted (or already exist due to legacy versioned-location decoding) such that `TokenIdOf::convert_location` produces the same `TokenId` for both — the exact bug class the outbound fix and its regression test target — then on the inbound v2 path a message carrying that colliding `token_id` would cause `ConvertAssetId::maybe_convert` to resolve to the *wrong* registered `Location`. The subsequent `AssetTransfer::ReserveWithdraw` would withdraw/settle the wrong reserve asset on AssetHub against the sovereign holding, i.e. an asset registered by/for one party gets moved using an identifier collision, mirroring the GiantPool bug's "wrong LP token accepted as equivalent to the tracked one." This falls squarely in the "forged or mis-bound proof or state acceptance" / "theft or unbacked mint or unlock" impact category for bridge message processing.

### Likelihood Explanation
Exploitability hinges entirely on the existence of a genuine `TokenId` collision in the encoding used by `TokenIdOf`. The presence of a purpose-built regression test for exactly this collision class on the outbound side is direct repository evidence that such collisions were achievable for at least one location-encoding pattern (`GeneralKey` length variants) before that specific fix. Because the inbound v2 path never received the matching defense-in-depth check, any future occurrence of a comparable collision — new junction types, versioned-location differences, reanchoring edge cases — would be silently exploitable inbound even though the outbound path is hardened. This is a genuine asymmetry/gap rather than a hardened, fully mitigated path.

### Recommendation
Add the same round-trip verification used in the outbound v2 converter to the inbound v2 `ForeignTokenERC20` handling:
```rust
let asset_location = ConvertAssetId::maybe_convert(*token_id)
    .ok_or(ConvertMessageError::InvalidAsset)?;
let recomputed_token_id = TokenIdOf::convert_location(&asset_location)
    .ok_or(ConvertMessageError::InvalidAsset)?;
ensure!(recomputed_token_id == *token_id, ConvertMessageError::InvalidAsset);
```
Additionally, audit `TokenIdOf::convert_location` for any remaining location-encoding ambiguity (the same class fixed for `GeneralKey` lengths) to ensure the hash is truly injective over all supported `Location` shapes, and add an inbound-side regression test mirroring `xcm_converter_mints_registered_token_id_for_colliding_general_key_location`.

### Proof of Concept
Conceptual reproduction (mirrors the existing outbound collision test, applied to the inbound path):
1. Register two distinct locations, `victim_location` (a legitimately-registered PNA reserve) and `attacker_location`, that are shown/constructed to collide under `TokenIdOf::convert_location` (as already demonstrated feasible for `GeneralKey`-style locations in `outbound-queue/src/v2/converter/tests.rs`).
2. Craft an inbound V2 Ethereum message containing `EthereumAsset::ForeignTokenERC20 { token_id: attacker_token_id, value }`, where `attacker_token_id` collides with `victim_token_id`.
3. Submit via `EthereumInboundQueueV2::process_message`; `MessageToXcm::prepare` calls `ConvertAssetId::maybe_convert(attacker_token_id)`, which — absent a round-trip check — can resolve to `victim_location`'s reserve.
4. The resulting XCM issues `WithdrawAsset` against the victim's registered reserve location instead of failing, withdrawing/settling the wrong underlying asset — the direct analog of withdrawing the attacker's worthless LP token against the victim's real, tracked GiantLP share. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L178-181)
```rust
			// Ensure PNA already registered
			let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
			let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
			ensure!(asset_id == expected_asset_id, InvalidAsset);
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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L181-198)
```rust
				EthereumAsset::ForeignTokenERC20 { token_id, value } => {
					let asset_location = ConvertAssetId::maybe_convert(*token_id)
						.ok_or(ConvertMessageError::InvalidAsset)?;
					let asset_hub_from_ethereum: Location = Location::new(
						1,
						[
							GlobalConsensus(LocalNetwork::get()),
							Parachain(AssetHubParaId::get().into()),
						],
					);
					let ethereum_universal: InteriorLocation =
						[GlobalConsensus(EthereumNetwork::get())].into();
					let reanchored_asset_location = asset_location
						.reanchored(&asset_hub_from_ethereum, &ethereum_universal)
						.map_err(|_| ConvertMessageError::CannotReanchor)?;
					let asset: Asset = (reanchored_asset_location, *value).into();
					assets.push(AssetTransfer::ReserveWithdraw(asset));
				},
```
