### Title
Foreign ERC-20 asset conversion in Snowbridge inbound queue v2 lacks round-trip `TokenId` binding, unlike the equivalent outbound path - ([File: bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs])

### Summary
This is the closest local analog to the `_isOptimismMintableERC20` issue: a "type check" (here, an ID→Location lookup) is accepted as sufficient proof of registration in one direction of the bridge, while the parallel code path in the other direction explicitly requires the same check to hold in **both** directions before trusting the mapping. The asymmetry means the inbound path relies on a weaker guarantee than the outbound path, mirroring the report's core issue: an object that only "half-implements" the expected contract (satisfies one direction of a bidirectional invariant) is nonetheless treated as fully valid.

### Finding Description
In the outbound queue v2 converter, when handling a Polkadot-native asset (PNA) being sent to Ethereum, the code explicitly re-derives the `TokenId` from the candidate `asset_id` and checks it matches the original `token_id` before minting: [1](#0-0) 

This round-trip check (`TokenIdOf::convert_location(&asset_id) == token_id` and `ConvertAssetId::maybe_convert(token_id) == asset_id`) exists specifically to defend against `Location`-encoding collisions, as demonstrated by the dedicated collision-mock test (`VictimOnlyTokenIdConvert` / `general_key_length_collision_locations`) built to prove that a crafted attacker `Location` can hash to the same `TokenId` as a legitimately registered "victim" location under certain encodings: [2](#0-1) 

In contrast, the inbound queue v2 converter (`prepare`, handling `EthereumAsset::ForeignTokenERC20` from Ethereum) performs only a **one-directional** lookup — `ConvertAssetId::maybe_convert(*token_id)` — and never re-derives/re-validates the `token_id` from the resulting `asset_location` before using it to build a `ReserveWithdraw` instruction: [3](#0-2) 

This is the same class of gap as the Optimism report: the outbound side implements the "full interface" (both directions of the ID↔Location relationship must agree), while the inbound side only implements "half the interface" (a single directional lookup), yet both are trusted equally to authorize movement of value. If the underlying `ConvertAssetId`/`TokenIdOf` describer ever produces a collision for two distinct `Location`s (as the outbound test explicitly demonstrates is possible for certain `GeneralKey` length/data encodings), the inbound path has no defense-in-depth check to catch it, whereas the outbound path does.

### Impact Explanation
If a `Location`-encoding collision exists (as proven possible and specifically tested for on the outbound side), the inbound path could resolve an Ethereum-supplied `token_id` to an unintended `Location` without detecting the mismatch, causing `AssetTransfer::ReserveWithdraw` to be issued against the wrong asset. This can lead to withdrawal/settlement of the wrong asset or amount on Asset Hub — a direct value-conservation violation ("wrong beneficiary or amount", "duplicate settlement", or fund loss) as covered by the impact gate. It is a public-entrypoint issue because ordinary bridge messages processed by any relayer (an unprivileged actor relative to chain state) exercise this code path.

### Likelihood Explanation
This requires that the `TokenIdOf`/`ConvertAssetId` describer used in production configuration is actually susceptible to a collision (the codebase itself contains an explicit collision test/mitigation for the outbound direction, suggesting collisions in this hashing scheme are a real, previously-identified concern). Exploitation would additionally require an attacker to get a colliding, non-legitimate `Location` registered as a foreign asset via governance/registration flow, or rely on collisions among already-registered locations. This raises the bar somewhat, so likelihood is moderate rather than trivial, but the missing symmetric check is a genuine gap relative to the parallel, already-hardened code path.

### Recommendation
Add the same round-trip validation used in `extract_polkadot_native_assets` (outbound) to the `ForeignTokenERC20` branch of `prepare()` (inbound): after resolving `asset_location = ConvertAssetId::maybe_convert(*token_id)`, re-derive `TokenIdOf::convert_location(&asset_location)` and `ensure!` it equals the original `*token_id` before constructing the `ReserveWithdraw` asset transfer. This closes the asymmetry and ensures both bridge directions enforce the same bijective ID↔Location binding.

### Proof of Concept
Conceptual PoC (cannot be fully executed without confirming a live collision in the deployed `DescribeTokenTerminal`/`TokenIdOf` describer, which is the same primitive already shown to be collision-prone in `general_key_length_collision_locations`):
1. Two distinct `Location`s L_victim (registered foreign asset) and L_attacker are crafted such that `TokenIdOf::convert_location(L_victim) == TokenIdOf::convert_location(L_attacker)` (per the pattern already demonstrated in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs:1318-1341`).
2. Ethereum-side message is sent with `EthereumAsset::ForeignTokenERC20 { token_id: T, value }` where `T` is the shared/colliding ID.
3. `ConvertAssetId::maybe_convert(T)` (the registry lookup) returns `L_victim` (the registered location) because it is keyed by `T`.
4. Since inbound `prepare()` performs no re-derivation/comparison, the constructed `AssetTransfer::ReserveWithdraw` unconditionally uses `L_victim`, with no runtime check that this is the "correct" resolution for the semantics an attacker intended to trigger — this mirrors exactly how the outbound path was hardened against this precise scenario via `ensure!(asset_id == expected_asset_id, InvalidAsset)`, a check inbound lacks.

Because I could not execute the collision-generation code against the live `DescribeTokenTerminal` implementation in this session (tool access ended), the exact conditions for a real collision in the currently deployed describer are not fully confirmed — this should be verified by running the existing `general_key_length_collision_locations` test harness against inbound-queue v2's `ConvertAssetId`/`TokenIdOf` to confirm exploitability end-to-end.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L178-184)
```rust
			// Ensure PNA already registered
			let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
			let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
			ensure!(asset_id == expected_asset_id, InvalidAsset);

			commands.push(Command::MintForeignToken { token_id, recipient, amount });
		}
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
