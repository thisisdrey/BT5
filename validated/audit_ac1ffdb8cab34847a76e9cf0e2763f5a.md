### Title
Arbitrary, unvalidated `decimals` in Snowbridge token registration lets the asset owner mis-declare the wrapped ERC20's decimals - ([File: bridges/snowbridge/pallets/system-frontend/src/lib.rs])

### Summary
`SnowbridgeSystemFrontend::register_token` accepts a caller-supplied `AssetMetadata { name, symbol, decimals }` and forwards `metadata.decimals` unchanged, through BridgeHub's `EthereumSystem::register_token` / `RegisterForeignToken` command, all the way to the `RegisterForeignToken` payload sent to the Ethereum Gateway, which uses it to instantiate the wrapped ERC20 contract's `decimals()` value. At no point is `metadata.decimals` checked against the real decimals of the underlying Polkadot asset (e.g. the value stored in `pallet_assets::Metadata` for that `asset_id`). This is the same broken invariant as the ERC4626i report: a component authoritative for token decimals uses a value that does not reflect the actual underlying asset, so any code that converts raw balances using `decimals()` will misrepresent the token amount by orders of magnitude.

### Finding Description
- `register_token` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` is callable by any origin whose `RegisterTokenOrigin` check passes for the given `asset_id` (asset owner or foreign asset owner - not privileged/root in general use), per the doc comment "All origins are allowed, however `asset_id` must be a location nested within the origin consensus system." [1](#0-0) 
- The `metadata: AssetMetadata` parameter, including `decimals: u8`, is taken as-is from the caller with no cross-check against the actual decimals configured for `asset_id` in `pallet-assets`/`pallet-balances`: [2](#0-1) 
- It is forwarded via `build_register_token_call` into a `Transact` to BridgeHub's `EthereumSystem::register_token`: [3](#0-2) 
- BridgeHub's `pallet-system` (or `system-v2`) `register_token`/`do_register_token` passes `metadata.decimals` straight into `Command::RegisterForeignToken { token_id, name, symbol, decimals }` with no validation: [4](#0-3) [5](#0-4) 
- `RegisterForeignToken.decimals` is the field ultimately used to instantiate the wrapped ERC20 contract on Ethereum: [6](#0-5) 

Every integration/emulated test in the repo hardcodes `decimals` matching the real asset (e.g. `decimals: 12` for WND, `decimals: 6` for USDT), confirming that correctness of this field is only enforced by test-author discipline, not by pallet logic: [7](#0-6) 

### Impact Explanation
Because `decimals` is baked once into the wrapped ERC20 contract at registration time and is never re-derived from the source asset's real metadata, a mismatch causes every subsequent balance interpretation of that wrapped token on Ethereum (wallets, DEX front-ends, other contracts computing `amount / 10**decimals`) to be wrong by many orders of magnitude — identical in effect to the ERC4626i bug where a fixed 18-decimals assumption made a real balance of 1 appear as "millions of trillions" of tokens. Whether the mismatch is registration-time attacker-chosen or a fixed constant, the corrupted value is the same: `decimals` used to render/convert `amount`, not the actual on-chain decimals of the backing asset.

### Likelihood Explanation
The `register_token` call in `system-frontend` is reachable by any account that is the owner of a `pallet-assets`/`ForeignAssets` asset (via `LocalAssetOwner`/`ForeignAssetOwner` `EnsureOriginWithArg`), so no privileged actor, governance, relayer, or validator is required — an asset owner who legitimately controls an asset's `asset_id` can simply pass an incorrect `decimals` value in the same call, since nothing forces `metadata.decimals` to equal `pallet_assets::Metadata::<T>::get(asset_id).decimals`. [8](#0-7) 

### Recommendation
In `do_register_token` (or the `system-frontend` `register_token` handler), fetch the authoritative decimals for `asset_id` from the asset's own metadata storage (`pallet_assets::Pallet::<T,I>::get_metadata(asset_id)` / native token decimals for the relay chain) rather than trusting the caller-supplied `AssetMetadata.decimals`, and reject registration (or overwrite the field) if the supplied value does not match.

### Proof of Concept
1. Asset owner (unprivileged) owns a `ForeignAssets`/`Assets` asset with real decimals = 12 (as configured in `pallet_assets::Metadata`).
2. They call `SnowbridgeSystemFrontend::register_token(origin, asset_id, AssetMetadata { name, symbol, decimals: 0 }, fee_asset)` — no check compares `0` against the real `12`. [9](#0-8) 
3. The XCM `Transact` reaches BridgeHub's `EthereumSystem::register_token`, which accepts the mismatched metadata unconditionally and emits `Command::RegisterForeignToken { decimals: 0, ... }`. [10](#0-9) 
4. The Gateway on Ethereum instantiates the wrapped ERC20 with `decimals() == 0`; any subsequent mint of the real (12-decimal) balance is then displayed/interpreted on Ethereum-side tooling as a value 10^12 times larger than the true amount, mirroring the ERC4626i misrepresentation bug exactly.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L210-252)
```rust
		/// Initiates the registration for a Polkadot-native token as a wrapped ERC20 token on
		/// Ethereum.
		/// - `asset_id`: Location of the asset
		/// - `metadata`: Metadata to include in the instantiated ERC20 contract on Ethereum
		///
		/// All origins are allowed, however `asset_id` must be a location nested within the origin
		/// consensus system.
		#[pallet::call_index(1)]
		#[pallet::weight(
			T::WeightInfo::register_token()
				.saturating_add(T::BackendWeightInfo::transact_register_token())
				.saturating_add(T::BackendWeightInfo::do_process_message())
				.saturating_add(T::BackendWeightInfo::commit_single())
				.saturating_add(T::BackendWeightInfo::submit_delivery_receipt())
		)]
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L319-338)
```rust
		// Build the call to dispatch the `EthereumSystem::register_token` extrinsic on BH
		fn build_register_token_call(
			sender: Location,
			asset: Location,
			metadata: AssetMetadata,
			amount: u128,
		) -> Result<BridgeHubRuntime<T>, Error<T>> {
			// reanchor locations relative to BH
			let sender = Self::reanchored(sender)?;
			let asset = Self::reanchored(asset)?;

			let call = BridgeHubRuntime::EthereumSystem(EthereumSystemCall::RegisterToken {
				sender: Box::new(VersionedLocation::from(sender)),
				asset_id: Box::new(VersionedLocation::from(asset)),
				metadata,
				amount,
			});

			Ok(call)
		}
```

**File:** bridges/snowbridge/primitives/core/src/lib.rs (L166-172)
```rust
/// Metadata to include in the instantiated ERC20 token contract
#[derive(Clone, Encode, Decode, DecodeWithMemTracking, PartialEq, Debug, TypeInfo)]
pub struct AssetMetadata {
	pub name: BoundedVec<u8, ConstU32<METADATA_FIELD_MAX_LEN>>,
	pub symbol: BoundedVec<u8, ConstU32<METADATA_FIELD_MAX_LEN>>,
	pub decimals: u8,
}
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L391-407)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			location: Box<VersionedLocation>,
			metadata: AssetMetadata,
		) -> DispatchResultWithPostInfo {
			ensure_root(origin)?;

			let location: Location =
				(*location).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			Self::do_register_token(&location, metadata, PaysFee::<T>::No)?;

			Ok(PostDispatchInfo {
				actual_weight: Some(T::WeightInfo::register_token()),
				pays_fee: Pays::No,
			})
		}
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L476-509)
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

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};
			Self::send(SECONDARY_GOVERNANCE_CHANNEL, command, pays_fee)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.clone().into(),
				foreign_token_id: token_id,
			});

			Ok(())
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L111-121)
```rust
	/// Register foreign token from Polkadot
	RegisterForeignToken {
		/// ID for the token
		token_id: H256,
		/// Name of the token
		name: Vec<u8>,
		/// Short symbol for the token
		symbol: Vec<u8>,
		/// Number of decimal places
		decimals: u8,
	},
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L158-182)
```rust
#[test]
pub fn register_usdt_from_owner_on_asset_hub() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	set_up_eth_and_dot_pool();
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		let fees_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(1_000_000_000u128) };

		assert_ok!(
			<AssetHubWestend as AssetHubWestendPallet>::SnowbridgeSystemFrontend::register_token(
				RuntimeOrigin::signed(AssetHubWestendAssetOwner::get()),
				bx!(VersionedLocation::from(usdt_at_ah_westend())),
				AssetMetadata {
					name: "usdt".as_bytes().to_vec().try_into().unwrap(),
					symbol: "usdt".as_bytes().to_vec().try_into().unwrap(),
					decimals: 6,
				},
				fees_asset
			)
		);
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L50-71)
```rust
	type RegisterTokenOrigin = EitherOf<
		EitherOf<
			LocalAssetOwner<
				AssetIdForTrustBackedAssetsConvert<TrustBackedAssetsPalletLocation, Location>,
				Assets,
				AccountId,
				AssetIdForTrustBackedAssets,
				Location,
			>,
			ForeignAssetOwner<
				(
					FromSiblingParachain<parachain_info::Pallet<Runtime>, Location>,
					xcm_config::bridging::to_rococo::RococoAssetFromAssetHubRococo,
				),
				ForeignAssets,
				AccountId,
				LocationToAccountId,
				Location,
			>,
		>,
		EnsureRootWithSuccess<AccountId, RootLocation>,
	>;
```
