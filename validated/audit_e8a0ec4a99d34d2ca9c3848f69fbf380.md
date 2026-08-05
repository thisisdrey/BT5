Audit Report

## Title
`register_token` re-broadcasts attacker-controlled token metadata to Ethereum even when the token is already registered - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

## Summary
`Pallet::register_token` in `bridges/snowbridge/pallets/system-v2/src/lib.rs` only gates the `ForeignToNativeId` storage insert with a `contains_key` check, but unconditionally builds and sends the `Command::RegisterForeignToken` message (carrying caller-supplied `name`, `symbol`, `decimals`) to the Ethereum Gateway regardless of whether the token was already registered. This allows a legitimate asset owner (permitted by `RegisterTokenOrigin`) to call `register_token` repeatedly with different metadata for the same asset, re-triggering the outbound message every time. [1](#0-0) 

## Finding Description
In `register_token`, the code computes `token_id` and only inserts into `ForeignToNativeId` if not already present, but the `Command::RegisterForeignToken{token_id, name, symbol, decimals}` construction and `Self::send(...)` call sit outside that `if` block and execute unconditionally on every call: [2](#0-1) 

The call path originates from Asset Hub's `snowbridge-pallet-system-frontend::register_token`, whose doc explicitly states "All origins are allowed, however `asset_id` must be a location nested within the origin consensus system," and it forwards the call via XCM `Transact` to Bridge Hub after only checking `T::RegisterTokenOrigin::ensure_origin`. [3](#0-2) 

Critically, I verified the concrete `RegisterTokenOrigin` wiring in `asset-hub-westend`: it is `EitherOf<EitherOf<LocalAssetOwner<...>, ForeignAssetOwner<...>>, EnsureRootWithSuccess<...>>`. [4](#0-3)  Both `LocalAssetOwner` and `ForeignAssetOwner` in `bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs` explicitly check that the calling account is the on-chain **owner** of the asset (via `AssetInspect::owner(...)` compared against the converted origin account), not just that the location is nested within the caller's namespace. [5](#0-4) [6](#0-5) 

This means the true reachable actor is the legitimate on-chain **owner of an already-registered trust-backed or foreign asset** — an unprivileged, ordinary asset-owning account, not a random attacker with no relationship to the asset. That owner can call `register_token` a second (or Nth) time with different `metadata` for the same `asset_id`. `ForeignToNativeId::contains_key(token_id)` will be `true`, so the storage insert is skipped, but the `Command::RegisterForeignToken` is still built with the new metadata and dispatched to Bridge Hub's `EthereumSystemV2::register_token`, which itself repeats the identical unconditional-send pattern before forwarding to the Ethereum Gateway. [1](#0-0)  Neither the frontend origin check nor the backend `FrontendOrigin::ensure_origin` on Bridge Hub verifies that submitted metadata matches a prior registration for the same `token_id`. [7](#0-6) 

## Impact Explanation
If the Ethereum Gateway contract updates the wrapped ERC-20's metadata idempotently on receiving `RegisterForeignToken` for an already-registered `token_id` (rather than rejecting duplicates), the asset owner can change `decimals`/`name`/`symbol` of an already-circulating wrapped token that other holders, wallets, DEXs, and integrators depend on, without their consent — matching the "runtime bug that compromises intended behavior" category. This is a real but narrower-scoped version of the submitted claim: the actor able to trigger this is not an arbitrary unprivileged attacker but specifically the legitimate owner of the affected asset, since `RegisterTokenOrigin` in the concrete runtime wiring is `LocalAssetOwner`/`ForeignAssetOwner`, both of which enforce strict asset ownership. [4](#0-3) 

The claim's assertion that "any account entitled to register an asset location nested in their own namespace" can trigger this is **not accurate for the production runtime configuration** — actual reachability requires being the asset's registered owner, which is a materially narrower condition than the claim implies (the claim treats the frontend doc comment about "nested within origin consensus" as the operative check, when in fact the wired `EnsureOriginWithArg` implementation additionally enforces asset ownership). The Solidity Gateway's actual duplicate-registration handling (idempotent-overwrite vs. reject) could not be verified in this repository, since Gateway contract source is outside the indexed scope; this is required to fully confirm on-chain impact.

## Likelihood Explanation
Reachable only by the legitimate owner of an already-registered trust-backed or foreign asset on Asset Hub — a normal, unprivileged asset owner, not governance, but also not an arbitrary/unrelated attacker. No relayer, validator, or governance action is required; a single signed extrinsic suffices to trigger the duplicate outbound dispatch to Bridge Hub and onward toward Ethereum. Confirmation of actual on-Ethereum impact (whether the Gateway overwrites vs. rejects) is unverified in-repo.

## Recommendation
In both `snowbridge-pallet-system::register_token`/`do_register_token` and `snowbridge-pallet-system-v2::register_token`, move the `Command::RegisterForeignToken` construction and `Self::send` call inside the `if !ForeignToNativeId::<T>::contains_key(token_id)` branch, or explicitly return an `AlreadyRegistered` error when the token is already registered and supplied metadata differs from the stored value, so the existence guard also prevents the external mutation, not just the internal bookkeeping write.

## Proof of Concept
1. Asset owner calls Asset Hub's `SnowbridgeSystemFrontend::register_token(asset_id, metadata_A, fee)`; `LocalAssetOwner`/`ForeignAssetOwner` verifies the caller is the asset's owner. [4](#0-3) 
2. This forwards via XCM `Transact` to Bridge Hub's `EthereumSystemV2::register_token`, which inserts `token_id -> location` into `ForeignToNativeId` (first time) and sends `Command::RegisterForeignToken{token_id, name_A, symbol_A, decimals_A}` to the Gateway. [8](#0-7) 
3. The same owner calls `register_token` again for the same `asset_id` with `metadata_B` (different `decimals`).
4. `ForeignToNativeId::contains_key(token_id)` is now `true` so the storage insert is skipped, but `Command::RegisterForeignToken{token_id, name_B, symbol_B, decimals_B}` is still built and sent unconditionally. [1](#0-0) 
5. If the Gateway updates the existing ERC-20's metadata on receipt rather than ignoring the duplicate registration, the circulating wrapped token's decimals/name/symbol change for all existing holders. This last step (Gateway behavior) could not be confirmed from this repository's index.

### Citations

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

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L13-53)
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
```

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L62-96)
```rust
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
