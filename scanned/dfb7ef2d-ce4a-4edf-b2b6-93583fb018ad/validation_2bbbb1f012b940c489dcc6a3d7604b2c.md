### Title
`register_token` re-registration re-arms the Ethereum `RegisterForeignToken` command with new metadata for an already-registered `token_id` - ([File: bridges/snowbridge/pallets/system-frontend/src/lib.rs])

### Summary
The core broken invariant in the external report is: a "set once" configuration path can be re-invoked, silently overwriting bound state that downstream logic assumes is immutable, without any explicit re-registration guard. The closest verified local analog is Snowbridge's token-registration flow (`register_token` in `pallet-system-frontend`, forwarded to `pallet-system-v2::register_token`, and the equivalent `pallet-system::register_token`/`do_register_token`): the local `ForeignToNativeId` idempotency check only guards the *local storage insert*, not the outbound `Command::RegisterForeignToken` message that is unconditionally re-sent to the Ethereum Gateway with whatever `metadata` (name/symbol/decimals) is supplied on that call.

### Finding Description
In `bridges/snowbridge/pallets/system/src/lib.rs` (`do_register_token`, lines 476-509) and mirrored in `bridges/snowbridge/pallets/system-v2/src/lib.rs` (`register_token`, lines 202-249), the guard is: [1](#0-0) 
```
let token_id = TokenIdOf::convert_location(&location)...
if !ForeignToNativeId::<T>::contains_key(token_id) {
    ForeignToNativeId::<T>::insert(token_id, location.clone());
}
let command = Command::RegisterForeignToken { token_id, name, symbol, decimals };
Self::send(SECONDARY_GOVERNANCE_CHANNEL, command, pays_fee)?;
```
The `contains_key` check only prevents a *duplicate storage write*; it does not prevent the `Command::RegisterForeignToken` from being sent again. Every call to `register_token` for the same `asset_id`/`token_id` re-sends a fresh `RegisterForeignToken` command to the Ethereum Gateway contract, carrying whatever `name`, `symbol`, and `decimals` were passed in that invocation.

`pallet-system-v2::register_token` is reachable from `pallet-system-frontend::register_token`, which is explicitly documented as permissionless: [2](#0-1) 
```
/// Initiates the registration for a Polkadot-native token as a wrapped ERC20 token on
/// Ethereum.
/// ...
/// All origins are allowed, however `asset_id` must be a location nested within the origin
/// consensus system.
...
let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;
```
So any origin that owns/controls a location nested under `asset_location` (e.g., an asset issuer/parachain sovereign for its own asset) can call `register_token` an arbitrary number of times for the *same* `asset_id`, each time supplying different `metadata` (in particular a different `decimals` value), and each call unconditionally triggers a new `RegisterForeignToken` governance-channel message to Ethereum — the Solidity Gateway side is not present in this repo, so its exact re-registration handling cannot be directly verified here, but nothing on the Substrate side prevents re-arming this command once a `token_id` already exists.

### Impact Explanation
`decimals` is used to scale amounts when converting between Polkadot-side balances and the wrapped ERC20 on Ethereum. If the Gateway contract accepts a second `RegisterForeignToken` for an already-known `token_id` and updates the token's `decimals` (or if any downstream code path assumes decimals are fixed at first registration), a re-registration with a different `decimals` value would silently change the scale factor applied to all subsequent transfers of that token across the bridge. This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant: users bridging the same token before and after a re-registration would receive systematically wrong amounts (over- or under-crediting), i.e. fund loss or unbacked value creation for/against arbitrary holders of that wrapped asset, not just the party that called `register_token`.

### Likelihood Explanation
The Substrate-side call is realistically reachable without any privileged origin: `pallet-system-frontend::register_token` is designed to be usable by any origin that controls the underlying asset location, and its only local defense (`ForeignToNativeId::contains_key`) does not block resubmission of the governance command. This differs from the excluded "admin abuse" pattern in the source report because the caller here is an ordinary asset owner, not a governance/root actor — the same class of actor who is expected to use this extrinsic legitimately. The residual uncertainty is on the Ethereum Gateway contract's actual handling of a duplicate `RegisterForeignToken` for an existing `tokenID`, which is outside this repository and could not be verified with the available tools.

### Recommendation
Make the check authoritative for the whole registration path, not just for the storage write: if `ForeignToNativeId::contains_key(token_id)` is true, return an error (e.g. `Error::<T>::TokenAlreadyRegistered`) instead of silently allowing the call to proceed to `Self::send(...)`. Apply the same guard in `pallet-system-v2::register_token` and in `pallet-system::do_register_token`. If intentional metadata updates are ever required, expose that as an explicit, separately-permissioned "update token metadata" extrinsic rather than allowing it as a side effect of re-calling `register_token`.

### Proof of Concept
1. Actor A (any account controlling `asset_location = X`) calls `pallet-system-frontend::register_token(asset_id = X, metadata = {decimals: 12, ...}, fee_asset)`. This reaches `pallet-system-v2::register_token`, which reanchors `X`, computes `token_id`, inserts into `ForeignToNativeId` (first time), and sends `Command::RegisterForeignToken{token_id, decimals: 12, ...}` to Ethereum.
2. Later, the same actor (or any other origin satisfying `T::RegisterTokenOrigin`/`T::FrontendOrigin` for location `X`) calls `register_token` again for the same `asset_id = X` with `metadata = {decimals: 6, ...}`.
3. In `pallet-system-v2::register_token`, `ForeignToNativeId::contains_key(token_id)` is now `true`, so the local mapping insert is skipped — but the function still proceeds to build and send a second `Command::RegisterForeignToken{token_id, decimals: 6, ...}`. [3](#0-2) 
4. If the Ethereum Gateway processes this second command by updating the existing token's decimals (unverifiable from this repo, as the contract code is not present here), all subsequent transfers of `token_id` are scaled incorrectly relative to balances established before the change, producing incorrect settlement amounts for other users of the same wrapped asset.

### Citations

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L488-501)
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
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L210-235)
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
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L225-242)
```rust
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

```
