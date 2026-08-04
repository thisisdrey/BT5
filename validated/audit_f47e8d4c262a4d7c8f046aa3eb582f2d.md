## Title
Missing duplicate-registration guard in Snowbridge `register_token` allows repeated `RegisterForeignToken` bridge commands for an already-registered asset - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`, `bridges/snowbridge/pallets/system/src/lib.rs`)

### Summary
The external report's core broken invariant is: an "initialize"-style entrypoint accepts an asset identifier and unconditionally proceeds with value-affecting bookkeeping (total-asset accounting), without checking whether that asset was already registered, so the same asset can be counted/registered more than once. The exact analog exists in Snowbridge's token-registration path: `register_token` (callable by any account that owns the asset, via the unprivileged `system-frontend` pallet) only conditionally skips the `ForeignToNativeId` storage insert if the token is already known, but it never gates the actual bridge action — it always builds and sends a fresh `Command::RegisterForeignToken` to the Ethereum Gateway and always emits `RegisterToken`, regardless of whether the token has already been registered.

### Finding Description
`do_register_token` in `bridges/snowbridge/pallets/system/src/lib.rs` computes the `token_id` for a location and only conditionally inserts into `ForeignToNativeId`: [1](#0-0) 

Note that the `if !ForeignToNativeId::<T>::contains_key(token_id)` check only guards the *storage insert*; there is no `ensure!`/early-return on the "already registered" branch. Execution falls through unconditionally to build `Command::RegisterForeignToken` and call `Self::send(...)`, dispatching the command to the Ethereum Gateway and depositing the `RegisterToken` event again.

The same pattern exists in the v2/user-facing path, `bridges/snowbridge/pallets/system-v2/src/lib.rs`: [2](#0-1) 

Critically, this v2 `register_token` is reachable by an ordinary, unprivileged user through `snowbridge-pallet-system-frontend`'s `register_token` extrinsic on AssetHub, gated only by `RegisterTokenOrigin: EnsureOriginWithArg<..., Location>` which merely checks the caller is the *owner* of the asset location being registered (see `ForeignAssetOwner`): [3](#0-2) [4](#0-3) 

There is no check anywhere in this call chain for "is this asset already registered" before the message is dispatched. Any asset owner can therefore call `register_token` on an asset that is already registered (its own `token_id` already present in `ForeignToNativeId`), and the pallet will still burn/swap the required fee and forward a brand-new `Command::RegisterForeignToken` (same `token_id`, but freshly supplied `name`/`symbol`/`decimals` metadata) to the Ethereum Gateway, and emit a fresh `RegisterToken` event, exactly the "no check to prevent duplicate" pattern from the external report — the write path silently no-ops but the state-changing/bridge-affecting side effect (the outbound message and event) is not deduplicated.

### Impact Explanation
This breaks the intended single-registration invariant for `RegisterForeignToken`. The pallet-level storage mapping being idempotent gives a false sense of protection while the actual bridge message — the one that matters for cross-chain accounting/behavior — is re-emitted every time, with attacker-controlled metadata (`name`/`symbol`/`decimals`) that can differ from the original registration. This matches the "duplicate settlement" / "message queues, bridge markers, receipts... must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot: the on-chain "already registered" state and the outbound command stream diverge, so repeated `RegisterForeignToken` commands with possibly inconsistent metadata are delivered to the Ethereum side for a token that governance/DAO already registered once, undermining any assumption on that side that registration is a one-time, idempotent event.

### Likelihood Explanation
High for the code path reachability: any account owning (or able to satisfy `ForeignAssetOwner`/`RegisterTokenOrigin` for) an already-registered foreign asset can call `register_token` again at will — no root, no governance, no relayer/prover assumption is required. The only cost is the fee-asset swap-and-burn, which is a normal, paid user action, not underpriced spam, but it's sufficient to repeatedly trigger the "already registered" branch, proving that the `contains_key` guard is purely cosmetic w.r.t. the message send.

### Recommendation
Add an explicit guard that turns "already registered" into a hard error (or a no-op that also skips sending the command), e.g.:
```rust
ensure!(!ForeignToNativeId::<T>::contains_key(token_id), Error::<T>::TokenAlreadyRegistered);
ForeignToNativeId::<T>::insert(token_id, location.clone());
```
applied consistently in both `do_register_token` (`bridges/snowbridge/pallets/system/src/lib.rs`) and `register_token` (`bridges/snowbridge/pallets/system-v2/src/lib.rs`), so the `Command::RegisterForeignToken` dispatch and `RegisterToken` event are only produced on first registration, matching the storage-level dedup intent.

### Proof of Concept
1. Asset owner `A` calls (via `system-frontend`) `register_token(asset_id = X, metadata = M1, fee_asset = F)`.
   - `ForeignToNativeId[token_id(X)]` is set; `Command::RegisterForeignToken{token_id, name: M1.name, ...}` is sent to Ethereum; fee `F` is burned.
2. `A` (still owner of `X`) calls `register_token(asset_id = X, metadata = M2, fee_asset = F)` again.
   - The `contains_key` check is true, so the storage insert is skipped, **but** the function still proceeds to build and send `Command::RegisterForeignToken{token_id, name: M2.name, ...}` — a second, differently-parameterized registration command for the same `token_id` — and burns fee `F` again, with no error raised.
3. Repeat step 2 arbitrarily many times: the bridge emits an unbounded number of `RegisterForeignToken` commands for a single already-registered asset, each carrying independently chosen metadata, violating the intended one-time registration invariant that downstream (Ethereum-side) logic is likely to assume.

### Citations

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L488-509)
```rust
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
