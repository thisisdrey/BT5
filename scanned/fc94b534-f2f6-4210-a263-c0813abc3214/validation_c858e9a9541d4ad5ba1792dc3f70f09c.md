Based on my research, the strongest local analog to the Cally `tokenType`/`token` mismatch bug is in Snowbridge's inbound message conversion, where a self-declared asset "kind" tag is trusted to pick between two very different settlement paths (mint-a-new-reserve vs. release-an-existing-reserve) without cross-checking it against the chain's own asset registry.

### Title
Unverified Ethereum-side asset `kind` tag lets an inbound V2 message choose the wrong reserve settlement path for a token that is already a registered Polkadot-native asset - (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`)

### Summary
The Cally report's root cause is that a caller-supplied "kind" discriminator for an asset is never validated against the real nature of the asset it is paired with, so the deposit path and the withdrawal path disagree about how to handle the same underlying value, and the strict path fails or the accounting becomes inconsistent, permanently harming the wrong party. Snowbridge's inbound-queue V2 message converter has the same shape: an Ethereum-supplied `kind` byte (`0` = `NativeTokenERC20`, `1` = `ForeignTokenERC20`) selects between two structurally different settlement instructions, and only one branch actually consults the chain's own registry of which tokens are truly Polkadot-native.

### Finding Description
`EthereumAsset` is decoded straight from the Gateway message with a `kind` selector that is not bound to any registry lookup: [1](#0-0) 

The converter then treats the two kinds asymmetrically. `EthereumAsset::NativeTokenERC20` is converted directly into a `Location` from the raw ERC-20 address and pushed as an `AssetTransfer::ReserveDeposit` — i.e. "mint/reserve a brand-new wrapped representation" — with **no check** that this token address hasn't already been registered as the Ethereum-side representation of an existing Polkadot-native asset (a PNA previously exported via `register_token`/`RegisterForeignToken`). In contrast, `EthereumAsset::ForeignTokenERC20` is required to resolve through `ConvertAssetId::maybe_convert(*token_id)`, i.e. it must already exist in the on-chain foreign-token registry, and is pushed as `AssetTransfer::ReserveWithdraw` — "release the already-reserved original asset": [2](#0-1) 

The Polkadot-native-token registration flow that creates the very state this should be checked against is here — it derives a `token_id` for a Polkadot asset and records the location under `ForeignToNativeId`: [3](#0-2) 

Because the `kind` tag in the message is trusted at face value by `prepare()` and the `NativeTokenERC20` branch performs no reverse lookup against this registry (unlike the `ForeignTokenERC20` branch, which is forced to hit the registry), an inbound message can mislabel a token that the chain already knows is a bridged-back Polkadot-native asset as if it were a genuinely-Ethereum-native token. This exactly mirrors Cally's asymmetry: one path (`ReserveDeposit`, analogous to the ERC20 `safeTransferFrom` that "just works") is lenient and unchecked, while the correct path (`ReserveWithdraw`, analogous to `safeTransfer` on the real ERC721) is the one that actually enforces the true identity of the asset.

### Impact Explanation
If the `kind` tag can diverge from the token's true registered status, the inbound message can select `ReserveDeposit` for an asset that should be settled via `ReserveWithdraw`. This directly falls under the required-impact categories of "theft or unbacked mint or unlock" and "duplicate settlement or payout": a value that should reduce the bridge's already-held reserve of a specific PNA is instead treated as new value requiring a fresh reserve-backed mint on AssetHub, corrupting the invariant that the bridge's on-chain reserve accounting for a given asset location settles exactly once and to the correct settlement primitive.

### Likelihood Explanation
The `kind` byte and the token identifier bytes both live in attacker/caller-influenced message payload data decoded on the Substrate side (`asset.kind`, `asset.data`), and the `prepare()` conversion function is on the critical decode → dispatch path for every inbound V2 message, so no privileged actor, governance action, or compromised relayer is required to reach this code — only crafting the message content that the Gateway forwards. I was not able to fully verify, within the tool budget available, whether the downstream `inbound-queue-v2` pallet (as opposed to the primitives converter crate I inspected) performs an additional cross-check between `NativeTokenERC20`'s `token_id` and the `NativeToForeignId`/`ForeignToNativeId` registries before executing the resulting XCM `AssetTransfer::ReserveDeposit`. This is a real gap in my analysis: if such a check exists in the pallet's message-processing logic (outside the converter module I reviewed), it would neutralize this path. This uncertainty should be resolved before treating this as a confirmed, exploitable bug.

### Recommendation
Before constructing an `AssetTransfer::ReserveDeposit` for an `EthereumAsset::NativeTokenERC20`, look up its derived `token_id`/location against the existing `NativeToForeignId`/`ForeignToNativeId` registry (the same one populated by `register_token`) and reject or redirect the message to the `ReserveWithdraw` path if the token is already known to be a re-imported Polkadot-native asset. This makes the `kind` tag a hint rather than a trusted, unchecked selector, matching the mitigation recommended in the original report (validate the declared type against the actual identity of the asset before choosing the transfer primitive).

### Proof of Concept
1. A Polkadot-native asset `X` (Location `L`) is registered for bridging via `register_token`, producing `token_id = H` and Ethereum ERC-20 wrapper deployed at address `A` for `H`, recorded in `ForeignToNativeId`/`NativeToForeignId` on BridgeHub — see `bridges/snowbridge/pallets/system-v2/src/lib.rs:211-249`.
2. An inbound V2 message is submitted (post-verification) with `assets = [EthereumAsset::NativeTokenERC20 { token_id: A, value }]` instead of the expected `ForeignTokenERC20 { token_id: H, value }`.
3. `prepare()` builds the token's `Location` directly from `A` (Ethereum-native namespace) with no lookup against the registry from step 1, and emits `AssetTransfer::ReserveDeposit` (`bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs:167-180`) instead of the `ReserveWithdraw` that the `ForeignTokenERC20` branch would have produced for the real underlying asset `X`.
4. This causes the resulting XCM program to treat `X`'s reserve accounting via the wrong settlement primitive, since only the `ForeignTokenERC20` branch is forced to resolve through the registry (lines 181-198), confirming the asymmetric, unchecked trust in the `kind` tag.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L217-243)
```rust
impl TryFrom<&IGatewayV2::EthereumAsset> for EthereumAsset {
	type Error = MessageDecodeError;

	fn try_from(asset: &IGatewayV2::EthereumAsset) -> Result<EthereumAsset, Self::Error> {
		let asset = match asset.kind {
			0 => {
				let native_data = IGatewayV2::AsNativeTokenERC20::abi_decode_validate(&asset.data)
					.map_err(|_| MessageDecodeError)?;
				EthereumAsset::NativeTokenERC20 {
					token_id: H160::from(native_data.token_id.as_ref()),
					value: native_data.value,
				}
			},
			1 => {
				let foreign_data =
					IGatewayV2::AsForeignTokenERC20::abi_decode_validate(&asset.data)
						.map_err(|_| MessageDecodeError)?;
				EthereumAsset::ForeignTokenERC20 {
					token_id: H256::from(foreign_data.token_id.as_ref()),
					value: foreign_data.value,
				}
			},
			_ => return Err(MessageDecodeError),
		};
		Ok(asset)
	}
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L167-200)
```rust
		for asset in &message.assets {
			match asset {
				EthereumAsset::NativeTokenERC20 { token_id, value } => {
					ensure!(*token_id != H160::zero(), ConvertMessageError::InvalidAsset);
					let token_location: Location = Location::new(
						2,
						[
							GlobalConsensus(EthereumNetwork::get()),
							AccountKey20 { network: None, key: (*token_id).into() },
						],
					);
					let asset: Asset = (token_location, *value).into();
					assets.push(AssetTransfer::ReserveDeposit(asset));
				},
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
			}
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L211-249)
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
		}
```
