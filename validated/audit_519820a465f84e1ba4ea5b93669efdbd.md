### Title
Permanent, Unconfirmed `ForeignToNativeId` Registration in Snowbridge System-V2 Can Permanently Brick a Token Pair - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`EthereumSystemFrontend::register_token` on AssetHub is a public, unprivileged extrinsic that any user can call for an asset located within their own consensus system. It relays the registration to `EthereumSystemV2::register_token` on BridgeHub, which permanently commits a `ForeignToNativeId` mapping in local storage *before* the corresponding `Command::RegisterForeignToken` message has actually been executed by the Gateway contract on Ethereum. Because the storage insert is guarded only by "insert-if-absent" and there is no extrinsic to update or remove a bad entry, any failure of the outbound message on the Ethereum side (insufficient fee/gas, Gateway-side rejection) leaves the pair permanently half-registered and unusable — an exact structural analog of the reported NFT/token-pair bug where "once a pair is registered, it cannot be updated/fixed" and cross-layer configuration can diverge.

### Finding Description
`system-frontend::register_token` is documented as open to all origins, subject only to the constraint that the asset is nested in the caller's own location: [1](#0-0) 

It reanchors the sender/asset locations and dispatches a `Transact` to BridgeHub's `EthereumSystem::register_token` (system-v2 pallet): [2](#0-1) 

On BridgeHub, `EthereumSystemV2::register_token` is guarded only by `T::FrontendOrigin::ensure_origin(origin)` (i.e., verifying the XCM came from the AH system-frontend pallet location), and then it unconditionally and permanently inserts into `ForeignToNativeId` *before* sending the outbound Ethereum command: [3](#0-2) 

The critical lines are:
```
if !ForeignToNativeId::<T>::contains_key(token_id) {
    ForeignToNativeId::<T>::insert(token_id, location.clone());
}
...
Self::send(message_origin, command, amount)?;
```
The `ForeignToNativeId` entry is committed to storage *before* — and independent of — whether `Command::RegisterForeignToken` is ever successfully delivered to and executed by the Gateway contract on Ethereum (this is a one-way, asynchronous outbound message with no delivery/execution confirmation loop back into this pallet). The identical pattern exists in the V1 `pallet-system::do_register_token`: [4](#0-3) 

The `contains_key` guard means the mapping, once written, can never be corrected or re-registered — there is no companion extrinsic anywhere in `snowbridge-pallet-system`/`system-v2` to remove or update a `ForeignToNativeId` entry (confirmed absent by repo-wide search for registration functions).

### Impact Explanation
This mirrors both concerns in the external report:
1. No verification that the eventual Ethereum-side state (ERC20 contract/registration) will actually match what BridgeHub commits — analogous to the missing "contract linkage" check between L1/L2.
2. The registration is performed and locally finalized on one layer (BridgeHub) optimistically, without any synchronized confirmation that the other layer (Ethereum Gateway) succeeded — exactly the scenario the audit team flagged as unresolved ("If the gas is insufficient... this would result in L1 contract having the registration while not on L2... This would make the bridge unusable").

Because the storage write happens before the cross-chain command is proven executed, and the entry can never be updated afterward, any token/location whose Ethereum-side registration fails (e.g., due to insufficient `amount`/fee for the outbound execution, Gateway contract-side validation failure, or congestion) becomes a permanently dead pair on BridgeHub — a `TokenId` reserved and unusable, with no remediation path. This is a permanent bridge-state lock for that asset, directly relevant to the "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot.

### Likelihood Explanation
The entry point (`EthereumSystemFrontend::register_token`) is explicitly documented as open to "all origins", requiring only that `asset_id` is nested within the caller's own consensus location — this is a normal, unprivileged, user-facing action (any parachain/account registering its own token for bridging). No malicious relayer, validator, or admin is required: an ordinary user or parachain triggering registration for their own legitimately-owned asset, combined with an under-provisioned execution fee/gas parameter (`amount`) or any transient failure on the Ethereum Gateway side, is sufficient to trigger the permanently-broken state. This requires no privileged actor and is directly reachable through the documented public dispatch path.

### Recommendation
Do not commit `ForeignToNativeId` (or the equivalent V1 storage) until confirmation of successful execution on Ethereum is received (e.g., via a receipt/ack mechanism analogous to delivery receipts used elsewhere in Snowbridge), or alternatively make the registration retryable/updatable if the corresponding outbound command has not been observed as executed within a bounded window. At minimum, add a governance-gated remediation extrinsic to clear/update a `ForeignToNativeId` entry so a failed cross-layer registration is not a permanent dead-end.

### Proof of Concept
1. A user calls `EthereumSystemFrontend::register_token(origin, asset_id, metadata, fee_asset)` on AssetHub for an asset nested in their own origin location, supplying a minimal/insufficient `amount`/fee for Ethereum-side execution.
2. This dispatches via XCM `Transact` to `EthereumSystemV2::register_token` on BridgeHub, which is validated only by `FrontendOrigin` (confirms the message came from the AH frontend pallet, not that the eventual Ethereum execution will succeed). [5](#0-4) 
3. `ForeignToNativeId::<T>::insert(token_id, location.clone())` is committed immediately and permanently (guarded only by `contains_key`), and `Command::RegisterForeignToken` is queued outbound to Ethereum.
4. If the outbound message fails to execute successfully on the Ethereum Gateway (insufficient gas/fee, contract-side rejection), the ERC20 side registration never completes, but the BridgeHub `ForeignToNativeId` entry for that `token_id`/`location` remains forever, and `contains_key` blocks any future re-registration attempt — permanently bricking that specific token/location for Snowbridge use with no available fix extrinsic.

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
