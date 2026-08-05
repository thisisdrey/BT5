## Title
Unlimited re-registration of a Polkadot-native token allows an unprivileged caller to overwrite the ERC20 metadata sent to the Ethereum Gateway - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-system-v2::register_token` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:211-249`) is reachable by any signed account on Asset Hub through the fully permissionless front-end wrapper `snowbridge-pallet-system-frontend::register_token` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:225-252`, explicitly documented as *"All origins are allowed"*). The `ForeignToNativeId` map is only guarded against being *overwritten*, but the `Command::RegisterForeignToken` message (which carries the ERC20 `name`/`symbol`/`decimals` that gets pushed to the Ethereum Gateway) is unconditionally re-sent on every call, with no bound on how many times it can be issued for the same `token_id`. This is the same broken invariant as the reported `L2WrappedBaseTokenStore` bug — "unlimited overwrite of a critical token-identity value with no rate limit or ownership check" — except here the actor does not need to be a privileged admin; any signed Asset-Hub account holding the relevant asset location can trigger it repeatedly.

### Finding Description
`register_token` in `system-v2`: [1](#0-0) 

```rust
pub fn register_token(...) -> DispatchResult {
    T::FrontendOrigin::ensure_origin(origin)?;
    ...
    let token_id = TokenIdOf::convert_location(&location)...;

    if !ForeignToNativeId::<T>::contains_key(token_id) {
        ForeignToNativeId::<T>::insert(token_id, location.clone());
    }

    let command = Command::RegisterForeignToken {
        token_id,
        name: metadata.name.into_inner(),
        symbol: metadata.symbol.into_inner(),
        decimals: metadata.decimals,
    };
    ...
    Self::send(message_origin, command, amount)?;
    ...
}
```

Only the *location* mapping (`ForeignToNativeId`) is protected from being overwritten (`contains_key` check). The `metadata` argument (name/symbol/decimals), supplied fresh by the caller on every invocation, is never checked against any previously-registered value, and the `Command::RegisterForeignToken` is dispatched to the outbound queue unconditionally — every call produces a new Gateway-bound message.

The entry point for reaching this call is the front-end pallet, which is documented as open to *all origins*: [2](#0-1) 

The only constraint enforced by `T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)` is that the `asset_id` location is "nested within the origin consensus system" — i.e., the caller must own/derive the asset location (e.g. it is their own sovereign sub-location or an asset they control), not that they are the original registrant of that `token_id` on Ethereum. Nothing prevents the same caller (or any other account whose location happens to reanchor to the same `token_id`) from calling `register_token` again and again with different `metadata` values.

Compare with the base `system` pallet's `register_token`, which is root-only and shows the intended trust model — remote governance is expected to be the only party allowed to (re)push token metadata to Ethereum: [3](#0-2) 

In `system-v2`, this privileged model was replaced with a permissionless, XCM-`Transact`-relayed origin, but the "send `RegisterForeignToken` unconditionally" behavior from the root-only version was carried over unchanged — losing the implicit governance-only guarantee without adding an explicit one-time-registration guard around the *command dispatch* (only the storage insert got one).

### Impact Explanation
Every accepted `register_token` call costs the caller only the swap/burn of a small "amount" of ether-denominated fee (`Self::swap_fee_asset_and_burn`) and pallet-weight fees; there is no per-`token_id` cap, cooldown, or "already registered" short-circuit before dispatching to the outbound queue. An attacker who controls (or can nest under) an asset location whose `token_id` has already been legitimately registered on Ethereum can repeatedly re-submit `RegisterForeignToken` with a different `name`/`symbol` (e.g. impersonating a well-known asset's branding, or corrupting `decimals`), causing the Ethereum Gateway/ERC20 metadata for that token to be updated arbitrarily and repeatedly. This directly maps to the "public underpriced work that degrades … bridge processing" and "runtime bugs that compromise intended behavior" impact categories: it lets an unprivileged user cause Snowbridge to push contradictory/attacker-chosen token identity data to Ethereum, an unbounded number of times, degrading trust in bridged token metadata and consuming outbound-queue capacity.

### Likelihood Explanation
High for the "repeated re-registration/spam" variant: any signed Asset Hub account that controls a matching asset location can call `register_token` as many times as they can afford the fee-burn, no origin escalation or governance action required. The metadata-overwrite aspect is bounded to callers who can produce a matching `asset_id`/`token_id` for an already-registered token — but nothing in the code path enforces "only the original registrant may re-register" or "only register once," so an attacker sharing/derivable-location access to the asset can trigger it.

### Recommendation
Track whether a `RegisterForeignToken` command has already been sent for a given `token_id` (not just whether the location mapping exists), and reject or explicitly gate subsequent metadata changes behind a privileged/governance origin (mirroring the root-gated `system` pallet), e.g.:
```rust
ensure!(!ForeignToNativeId::<T>::contains_key(token_id), Error::<T>::AlreadyRegistered);
ForeignToNativeId::<T>::insert(token_id, location.clone());
// only then send Command::RegisterForeignToken
```
This removes the ability for any caller to unconditionally re-issue `RegisterForeignToken` for the same token, closing the "unlimited overwrite" path.

### Proof of Concept
1. Caller A registers token `T` via `system-frontend::register_token(asset_id=T, metadata={name:"USD Coin", symbol:"USDC", decimals:6}, fee_asset)`. This dispatches through `Transact` XCM to BridgeHub's `system-v2::register_token`, which inserts `ForeignToNativeId[token_id] = location` and sends `Command::RegisterForeignToken{token_id, name:"USD Coin", symbol:"USDC", decimals:6}` to Ethereum.
2. Caller B (or A again), who can produce/derive the same nested `asset_id` location, calls `system-frontend::register_token(asset_id=T, metadata={name:"FAKE", symbol:"SCAM", decimals:0}, fee_asset)` again.
3. `ForeignToNativeId::contains_key(token_id)` is `true`, so the storage insert is skipped, but `Command::RegisterForeignToken{name:"FAKE", symbol:"SCAM", decimals:0}` is still built and sent to the outbound queue unconditionally, per `bridges/snowbridge/pallets/system-v2/src/lib.rs:229-241`.
4. Step 2 can be repeated indefinitely, each time re-pushing new metadata for the same `token_id` to the Gateway on Ethereum, with no guard rejecting the resend.

Note: I was unable to fully trace the exact `T::RegisterTokenOrigin` runtime implementation used on Asset Hub Westend (only its trait bound was found in the index), so the precise class of accounts able to supply a "nested" `asset_id` for an *already-registered* token could not be fully enumerated from the available code — this should be verified in a live session by inspecting the concrete `RegisterTokenOrigin` type wired in the Asset Hub runtime config.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L217-249)
```rust
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L381-407)
```rust
		/// Registers a Polkadot-native token as a wrapped ERC20 token on Ethereum.
		/// Privileged. Can only be called by root.
		///
		/// Fee required: No
		///
		/// - `origin`: Must be root
		/// - `location`: Location of the asset (relative to this chain)
		/// - `metadata`: Metadata to include in the instantiated ERC20 contract on Ethereum
		#[pallet::call_index(10)]
		#[pallet::weight(T::WeightInfo::register_token())]
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
