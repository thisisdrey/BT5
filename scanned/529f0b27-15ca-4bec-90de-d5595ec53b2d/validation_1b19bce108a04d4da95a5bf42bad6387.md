Based on the investigation, the strongest local analog to the `addAsset` overwrite bug is in the Snowbridge V2 system pallet's `register_token` extrinsic.

### Title
`register_token` re-broadcasts attacker-controlled token metadata to Ethereum even when the token is already registered - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
### Finding Description
`Pallet::register_token` in [1](#0-0)  guards the `ForeignToNativeId` storage map with an existence check, but that check only protects the reverse-lookup mapping — it does **not** gate the outbound `Command::RegisterForeignToken` message that carries the caller-supplied `name`, `symbol`, and `decimals`:

```rust
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
```

Whether or not `token_id` is already registered, the pallet unconditionally builds and dispatches the `RegisterForeignToken` command to the Ethereum Gateway with whatever `metadata` was supplied in *this* call. The `contains_key` check gives the false appearance that re-registration is a no-op (exactly like the `addAsset`/`assetInfoOf[_asset]` pattern from the external report), while the actual state-mutating side effect — the message sent to the bridge that instructs the Gateway contract to (re)configure the wrapped ERC-20's metadata — still fires every time.

The call path originates from the permissionless, user-facing `register_token` in the system-frontend pallet on Asset Hub [2](#0-1) , whose own doc comment states "All origins are allowed, however `asset_id` must be a location nested within the origin consensus system" (`T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)`). This nesting check only constrains *which* asset locations a caller may name — it does not prevent the same caller (or, depending on the concrete `RegisterTokenOrigin` implementation, any account that can produce a location nested under the asset's own namespace) from calling `register_token` a second time with different `metadata` for a location it already registered.

On the Bridge Hub side, `FrontendOrigin::ensure_origin(origin)` in `register_token` [3](#0-2)  only verifies that the XCM `Transact` arrived via the trusted frontend pallet's descended origin — it performs no check on whether the encoded `metadata` differs from a previous registration for the same `token_id`.

### Impact Explanation
If the Ethereum Gateway contract processes `RegisterForeignToken` idempotently by updating the ERC-20 metadata (name/symbol/decimals) rather than rejecting duplicate registrations, an account entitled to register a token under its own namespace can repeatedly change the metadata of an already-registered, globally shared wrapped token. A `decimals` change in particular is not cosmetic: every other pallet, wallet, DEX, or off-chain integrator that reads decimals to compute value would silently miscompute balances for a token that many unrelated holders already possess — a direct analog to the "Asset Manager can update the aggregator of an existing asset, impacting all functions that make use of the asset" finding, except reachable without any privileged/governance role, purely through the public `register_token` entrypoint.

### Likelihood Explanation
Likely reachable by any account that owns (or is nested within) the namespace of an already-registered asset location, since `register_token` is explicitly documented as open to "all origins." No relayer, validator, or governance action is required — a single signed extrinsic on Asset Hub is sufficient to trigger the duplicate/overwrite dispatch to Bridge Hub and onward to Ethereum.

### Recommendation
In `do_register_token`/`register_token` (both `snowbridge-pallet-system` and `snowbridge-pallet-system-v2`), only build and send the `RegisterForeignToken` command when `ForeignToNativeId` does **not** already contain the `token_id` (i.e., move the `Command::RegisterForeignToken` construction/`send` call inside the `if !contains_key` branch, or explicitly reject with an `AlreadyRegistered` error if metadata differs from what's on record), so that the guard actually prevents the external mutation, not just the internal bookkeeping.

### Proof of Concept
1. Attacker (any account entitled to register an asset location nested in their own namespace) calls Asset Hub's `SnowbridgeSystemFrontend::register_token(asset_id, metadata_A, fee)`.
2. This forwards via XCM `Transact` to Bridge Hub's `EthereumSystemV2::register_token`, which inserts `token_id -> location` into `ForeignToNativeId` (first time) and sends `Command::RegisterForeignToken{token_id, name_A, symbol_A, decimals_A}` to the Gateway, which deploys/initializes the wrapped ERC-20.
3. Attacker calls `register_token` again for the **same** `asset_id` but with `metadata_B` (different `decimals`/`name`/`symbol`).
4. `ForeignToNativeId::contains_key(token_id)` is now `true`, so the storage insert is skipped — but `Command::RegisterForeignToken{token_id, name_B, symbol_B, decimals_B}` is still built and sent unconditionally.
5. If the Gateway updates the existing ERC-20's metadata on receipt (rather than ignoring a duplicate registration), the already-circulating wrapped token's decimals/name/symbol change under every existing holder without their consent.

**Note on uncertainty:** I could not fully verify, within the available tool budget, the exact concrete `RegisterTokenOrigin` implementation wired into `asset-hub-westend/src/bridge_to_ethereum_config.rs` (i.e., precisely how "nested within the origin consensus system" is enforced), nor whether the Ethereum-side Gateway contract's `registerToken` function is idempotent-overwrite or reject-on-duplicate. The Solidity Gateway contract source is not part of this repository's index, so its duplicate-registration handling could not be confirmed here — this would need to be checked in the Snowbridge contracts repo/deployment to fully confirm the on-chain impact. [1](#0-0) [2](#0-1)

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
