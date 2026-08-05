This confirms the claim precisely matches the code. `register_token` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` only enforces `T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)` via `ForeignAssetOwner`/`LocalAssetOwner` (in `bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs`), which checks asset ownership only — no decimals cross-check against the asset's real on-chain metadata exists anywhere in this path.Audit Report

## Title
Unvalidated caller-supplied `decimals` in `snowbridge-pallet-system-frontend::register_token` permanently mis-scales the wrapped ERC20 minted on Ethereum - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`EthereumSystemFrontend::register_token` on Asset Hub accepts a caller-supplied `AssetMetadata { name, symbol, decimals }` and forwards it unmodified through Bridge Hub's `snowbridge-pallet-system-v2::register_token` into `Command::RegisterForeignToken`, which instructs the Ethereum Gateway contract to deploy an immutable ERC20 with that `decimals` value. The only gating check is `RegisterTokenOrigin::ensure_origin`, which in the production Asset Hub Westend config (`EitherOf<EitherOf<LocalAssetOwner<...>, ForeignAssetOwner<...>>, EnsureRootWithSuccess<...>>`) verifies only that the caller owns/administers the referenced asset `Location` — it never cross-checks `metadata.decimals` against the asset's actual on-chain decimals via `pallet_assets::Inspect`.

## Finding Description
`register_token` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` performs `ensure!(!Self::export_operating_mode().is_halted(), ...)` and `T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?`, and passes `metadata` straight into `build_register_token_call` without any inspection of its fields. [1](#0-0) 

The origin check used in the real Asset Hub Westend runtime is `LocalAssetOwner`/`ForeignAssetOwner`, both of which only verify asset ownership (`AssetInspect::owner(...) == who`), with no decimals comparison anywhere in `try_origin`. [2](#0-1) 

This origin type is wired into the production config: [3](#0-2) 

The metadata is reanchored and forwarded via XCM `Transact` to `EthereumSystemCall::RegisterToken` unchanged. [4](#0-3) 

On Bridge Hub, `snowbridge-pallet-system-v2::register_token` is gated only by `T::FrontendOrigin::ensure_origin` (verifying the call came from the frontend pallet, not verifying metadata correctness) and forwards `metadata.decimals` verbatim into `Command::RegisterForeignToken`. [5](#0-4) 

`Command::RegisterForeignToken.decimals` is ABI-encoded directly into `RegisterForeignTokenParams.decimals` sent to the Ethereum Gateway, which uses it to deploy the wrapped ERC20 contract with that fixed decimals value. [6](#0-5) [7](#0-6) 

The integration tests confirm this reachability: a plain signed asset owner can freely choose `decimals: 6` for USDT (`register_usdt_from_owner_on_asset_hub`), and the *only* failure mode tested is a non-owner calling (`BadOrigin`), never an incorrect-decimals rejection (`register_usdt_not_from_owner_on_asset_hub_will_fail`). No test or code path anywhere in `system-frontend`, `system-v2`, or the origin implementations cross-references `metadata.decimals` against `pallet_assets::Inspect::decimals` or any authoritative source.

## Impact Explanation
Since ERC20 `decimals()` is immutable post-deployment and the raw transferred amount is not rescaled based on registered decimals, any caller-chosen mismatch between `metadata.decimals` and the real decimals of the underlying Polkadot asset permanently corrupts the value interpretation of every unit of that wrapped token on Ethereum for its entire lifetime. This falls under the impact gate's "runtime bugs that compromise intended behavior," since it is a value-scale corruption reachable via a public extrinsic without any privileged actor, relayer compromise, or governance abuse — the exact corrupted value is `Command::RegisterForeignToken.decimals` (equivalently the deployed ERC20's `decimals()`), which becomes permanently detached from the true decimals of the reserve asset.

## Likelihood Explanation
High. `register_token` is a standard permissionless (asset-owner-gated) extrinsic on Asset Hub. The origin check only verifies asset ownership, and the existing test suite (`register_usdt_from_owner_on_asset_hub`) already demonstrates the exact call shape needed — a signed asset owner supplying arbitrary `decimals` for a real, valuable asset (USDT). Any account that owns or creates any asset location (including a fresh, low-value asset via `pallet_assets::create`) can trigger this with no additional privilege.

## Recommendation
Before dispatching to `RegisterForeignToken`, validate `metadata.decimals` (and ideally `name`/`symbol`) against the authoritative on-chain metadata of the asset identified by `asset_location` — e.g., via `pallet_assets::Inspect::decimals` for `Assets`/`ForeignAssets`, or a fixed constant for the native `Balances` asset — and reject the call (e.g., with a new `Error::<T>::DecimalsMismatch`) if they do not match, rather than trusting caller-supplied values end to end through `system-frontend` → `system-v2` → `Command::RegisterForeignToken`.

## Proof of Concept
1. An account owning a freshly `pallet_assets::create`d local asset with real `decimals = 12` calls `SnowbridgeSystemFrontend::register_token` on Asset Hub Westend, passing `AssetMetadata { name, symbol, decimals: 0 }` and a valid `fee_asset`.
2. `LocalAssetOwner::try_origin` succeeds because the caller is the asset's registered owner; no field of `metadata` is checked. [8](#0-7) 
3. `build_register_token_call` and `send_transact_call` deliver an XCM `Transact` to Bridge Hub with `metadata.decimals == 0`. [4](#0-3) 
4. `snowbridge-pallet-system-v2::register_token` forwards `decimals: 0` into `Command::RegisterForeignToken` unchanged. [9](#0-8) 
5. The Ethereum Gateway deploys an ERC20 with `decimals() == 0` while the real asset has 12 decimals; every subsequent bridged transfer of raw amount `X` displays as `X` whole tokens on Ethereum instead of `X / 10^12`, a permanent 10^12 valuation distortion that can be reproduced deterministically in an integration test analogous to `register_usdt_from_owner_on_asset_hub` by substituting a controlled low-value asset and mismatched `decimals`.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-252)
```rust
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

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L13-96)
```rust
/// Origin check that verifies that an origin is the owner of a foreign asset.
/// 1. Allows XCM origins
/// 2. Checks that the asset exists
/// 3. The origin must be the owner of the asset
pub struct ForeignAssetOwner<IsForeign, AssetInspect, AccountId, LocationToAccountId, L = Location>(
	core::marker::PhantomData<(IsForeign, AssetInspect, AccountId, LocationToAccountId, L)>,
);

impl<
		IsForeign: ContainsPair<L, L>,
		AssetInspect: frame_support::traits::fungibles::roles::Inspect<AccountId>,
		AccountId: Eq + Clone,
		LocationToAccountId: xcm_executor::traits::ConvertLocation<AccountId>,
		RuntimeOrigin: From<XcmOrigin> + OriginTrait + Clone,
		L: From<Location> + Into<Location> + Clone,
	> EnsureOriginWithArg<RuntimeOrigin, L>
	for ForeignAssetOwner<IsForeign, AssetInspect, AccountId, LocationToAccountId, L>
where
	for<'a> &'a RuntimeOrigin::PalletsOrigin: TryInto<&'a XcmOrigin>,
	<AssetInspect as frame_support::traits::fungibles::Inspect<AccountId>>::AssetId: From<Location>,
{
	type Success = L;

	fn try_origin(
		origin: RuntimeOrigin,
		asset_location: &L,
	) -> Result<Self::Success, RuntimeOrigin> {
		let origin_location = EnsureXcm::<Everything, L>::try_origin(origin.clone())?;
		if !IsForeign::contains(asset_location, &origin_location) {
			return Err(origin);
		}
		let asset_location: Location = asset_location.clone().into();
		let owner = AssetInspect::owner(asset_location.into()).ok_or_else(|| origin.clone())?;
		let location: Location = origin_location.clone().into();
		let from =
			LocationToAccountId::convert_location(&location).ok_or_else(|| origin.clone())?;
		if from != owner {
			return Err(origin);
		}
		Ok(location.into())
	}

	#[cfg(feature = "runtime-benchmarks")]
	fn try_successful_origin(a: &L) -> Result<RuntimeOrigin, ()> {
		let latest_location: Location = (*a).clone().try_into().map_err(|_| ())?;
		Ok(pallet_xcm::Origin::Xcm(latest_location).into())
	}
}

/// Origin check that verifies that an origin is the owner of a local trusted asset.
/// 1. Allows signed origins
/// 2. Checks that the asset exists
/// 3. The origin must be the owner of the asset
pub struct LocalAssetOwner<MatchAssetId, AssetInspect, AccountId, AssetId, L = Location>(
	core::marker::PhantomData<(MatchAssetId, AssetInspect, AccountId, AssetId, L)>,
);

impl<
		MatchAssetId: MaybeEquivalence<L, AssetId>,
		AssetInspect: frame_support::traits::fungibles::roles::Inspect<AccountId>,
		AccountId: Eq + Clone + Into<L>,
		AssetId: Eq + Clone,
		RuntimeOrigin: OriginTrait + Clone,
		L: From<Location> + Into<Location> + Clone,
	> EnsureOriginWithArg<RuntimeOrigin, L>
	for LocalAssetOwner<MatchAssetId, AssetInspect, AccountId, AssetId, L>
where
	RuntimeOrigin: Into<Result<RawOrigin<AccountId>, RuntimeOrigin>> + From<RawOrigin<AccountId>>,
	<AssetInspect as frame_support::traits::fungibles::Inspect<AccountId>>::AssetId: From<AssetId>,
{
	type Success = L;

	fn try_origin(
		origin: RuntimeOrigin,
		asset_location: &L,
	) -> Result<Self::Success, RuntimeOrigin> {
		let who = ensure_signed(origin.clone()).map_err(|_| origin.clone())?;
		let asset_id = MatchAssetId::convert(asset_location).ok_or_else(|| origin.clone())?;
		let owner = AssetInspect::owner(asset_id.into()).ok_or_else(|| origin.clone())?;
		if who != owner {
			return Err(origin);
		}
		Ok(who.into())
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L45-71)
```rust
impl snowbridge_pallet_system_frontend::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type WeightInfo = weights::snowbridge_pallet_system_frontend::WeightInfo<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type Helper = ();
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L69-78)
```rust
		struct RegisterForeignTokenParams {
			/// @dev The token ID (hash of stable location id of token)
			bytes32 foreignTokenID;
			/// @dev The name of the token
			bytes name;
			/// @dev The symbol of the token
			bytes symbol;
			/// @dev The decimal of the token
			uint8 decimals;
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L228-236)
```rust
			Command::RegisterForeignToken { token_id, name, symbol, decimals } => {
				RegisterForeignTokenParams {
					foreignTokenID: FixedBytes::from(token_id.as_fixed_bytes()),
					name: Bytes::from(name.to_vec()),
					symbol: Bytes::from(symbol.to_vec()),
					decimals: *decimals,
				}
				.abi_encode()
			},
```
