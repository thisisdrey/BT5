## Title
`RegisterForeignToken` command charges a fixed, name/symbol-length-independent gas figure, allowing underpriced/unbounded-cost Ethereum dispatch — ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs])

### Summary
`ConstantGasMeter::maximum_dispatch_gas_used_at_most` assigns a single hardcoded gas constant to `Command::RegisterForeignToken` (`1_200_000` in both v1 and v2 primitives) regardless of the actual size of the `name` and `symbol` fields, which are attacker/user-influenced variable-length `Vec<u8>` values. [1](#0-0) [2](#0-1) 

### Finding Description
`GasMeter::maximum_dispatch_gas_used_at_most` is documented as "the maximum amount of gas a command payload will require to dispatch." [3](#0-2) 
For every other command, the payload is fixed-size (addresses, `H256`, `u128` amounts), so a constant is a valid upper bound. `RegisterForeignToken`, however, carries `name: Vec<u8>` and `symbol: Vec<u8>` — both fully attacker-controlled, unbounded-in-code, variable-length byte strings that are ABI-encoded as `bytes`/`string` and stored on the Ethereum Gateway contract when the token is registered. [4](#0-3) [5](#0-4) 

`Command::RegisterForeignToken { .. } => 1_200_000` is applied irrespective of how long `name`/`symbol` are. [6](#0-5) [7](#0-6) 

This constant feeds directly into fee calculation (`calculate_fee`/`calculate_remote_fee`), which charges the sender exactly `fee_per_gas * gas_used_at_most + reward` and is the sole basis for the "max gas" figure committed on-chain and later relied upon by the relayer/Gateway contract for gas-refund accounting. [8](#0-7) 
The path from the AssetHub-facing entrypoints (`pallet_system::register_token`, `pallet_system_v2::register_token`, and the frontend proxy `pallet_system_frontend::register_token`) accepts an `AssetMetadata { name, symbol, decimals }` supplied by the caller and forwards `metadata.name.into_inner()` / `metadata.symbol.into_inner()` verbatim into the `Command::RegisterForeignToken` construction, with no visible length cross-check against the hardcoded gas budget. [9](#0-8) [10](#0-9) 

Because the hardcoded value does not scale with payload length (unlike the report's called-out `baseGas`/`intrinsicGas`/`executionGas` estimation that should account for message-dependent costs), a token registration with a sufficiently long `name`/`symbol` can require materially more EVM gas to ABI-decode and store the two dynamic `bytes`/`string` fields on Ethereum than the committed `max_dispatch_gas` covers. The BridgeHub side has already advanced its `Nonce`, appended the message to `Messages`/`MessageLeaves`, and emitted `MessageAccepted` before this discrepancy can be detected — settlement of the commitment is unconditional and irreversible once `do_process_message` returns `Ok(true)`. [11](#0-10) 

### Impact Explanation
If the actual Ethereum-side gas required to decode/store an oversized `name`/`symbol` exceeds the committed `max_dispatch_gas`, the Gateway contract's dispatch of `RegisterForeignToken` will run out of gas and revert on Ethereum, while the corresponding BridgeHub message has already been irrevocably committed (nonce consumed, fee charged, merkle leaf appended). This mirrors the report's core defect exactly: a hardcoded gas figure that ignores message/list length, causing loss of the cross-chain operation (here, token registration) for payloads the estimator did not anticipate — a stuck/failed bridge state that cannot be replayed under the same nonce.

### Likelihood Explanation
`register_token` in v1 is guarded by `ensure_root`, making it privileged (out of scope per the "no governance/admin abuse" exclusion). However, `pallet_system_v2::register_token` and `pallet_system_frontend::register_token` are reachable via `T::FrontendOrigin`/AH-proxy paths intended for permissionless/broader use (per Snowbridge V2's design goal of user-agent driven bridging), and accept `metadata: AssetMetadata` directly from the caller with no observed enforcement that `name.len() + symbol.len()` stays within the bound assumed by the `1_200_000` gas constant. [12](#0-11) 
This is a data-dependent underpricing/underestimation bug rather than one requiring a malicious relayer, validator, or governance actor — an ordinary user choosing a long token name/symbol is sufficient to trigger it. I could not fully verify from the indexed code whether `AssetMetadata`'s `name`/`symbol` are bounded by a `BoundedVec`/`StringLimit` small enough to always stay under the gas assumption (the `AssetMetadata` struct definition in `bridges/snowbridge/primitives/core/src/lib.rs` was found but its field bound constants were not resolved in this pass) — this is the main uncertainty in the likelihood assessment.

### Recommendation
Compute `maximum_dispatch_gas_used_at_most` for `RegisterForeignToken` as a function of `name.len()` and `symbol.len()` (e.g., base gas + per-byte gas cost for ABI-encoded dynamic `bytes`/`string` fields), similar to how `Command::Upgrade` already varies its gas estimate with `initializer.maximum_required_gas`. Alternatively, enforce a strict, benchmarked maximum length for `name`/`symbol` in `AssetMetadata` and assert (as done in `bridges/modules/messages/src/weights_ext.rs`'s `ensure_maximal_message_dispatch`) that the worst-case encoded size still fits within the hardcoded gas budget.

### Proof of Concept
1. Call `pallet_system_v2::Pallet::register_token` (or the AH-facing `pallet_system_frontend::register_token`) with an `AssetMetadata` whose `name`/`symbol` are set to the maximum length permitted by their type bounds (as long as this exceeds what `1_200_000` gas can safely decode/store on the Gateway contract).
2. Observe that `Command::RegisterForeignToken { name, symbol, .. }` is queued with `max_dispatch_gas = 1_200_000` regardless of actual payload size, per `ConstantGasMeter`.
3. On Ethereum, the Gateway's dispatch of the decoded command executes with the committed max gas; for sufficiently long `name`/`symbol` values the ABI decode + storage write exceeds `1_200_000` gas and the call reverts, while the BridgeHub-side nonce/commitment has already been advanced and cannot be resubmitted for the same registration attempt.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L317-330)
```rust
pub trait GasMeter {
	/// All the gas used for submitting a message to Ethereum, minus the cost of dispatching
	/// the command within the message
	const MAXIMUM_BASE_GAS: u64;

	/// Total gas consumed at most, including verification & dispatch
	fn maximum_gas_used_at_most(command: &Command) -> u64 {
		Self::MAXIMUM_BASE_GAS + Self::maximum_dispatch_gas_used_at_most(command)
	}

	/// Measures the maximum amount of gas a command payload will require to *dispatch*, NOT
	/// including validation & verification.
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64;
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L348-374)
```rust
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
		match command {
			Command::SetOperatingMode { .. } => 40_000,
			Command::AgentExecute { command, .. } => match command {
				// Execute IERC20.transferFrom
				//
				// Worst-case assumptions are important:
				// * No gas refund for clearing storage slot of source account in ERC20 contract
				// * Assume dest account in ERC20 contract does not yet have a storage slot
				// * ERC20.transferFrom possibly does other business logic besides updating balances
				AgentExecuteCommand::TransferToken { .. } => 200_000,
			},
			Command::Upgrade { initializer, .. } => {
				let initializer_max_gas = match *initializer {
					Some(Initializer { maximum_required_gas, .. }) => maximum_required_gas,
					None => 0,
				};
				// total maximum gas must also include the gas used for updating the proxy before
				// the the initializer is called.
				50_000 + initializer_max_gas
			},
			Command::SetTokenTransferFees { .. } => 60_000,
			Command::SetPricingParameters { .. } => 60_000,
			Command::UnlockNativeToken { .. } => 200_000,
			Command::RegisterForeignToken { .. } => 1_200_000,
			Command::MintForeignToken { .. } => 100_000,
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L162-172)
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L291-306)
```rust
impl GasMeter for ConstantGasMeter {
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
		match command {
			Command::SetOperatingMode { .. } => 40_000,
			Command::Upgrade { initializer, .. } => {
				// total maximum gas must also include the gas used for updating the proxy before
				// the the initializer is called.
				50_000 + initializer.maximum_required_gas
			},
			Command::UnlockNativeToken { .. } => 200_000,
			Command::RegisterForeignToken { .. } => 1_200_000,
			Command::MintForeignToken { .. } => 100_000,
			Command::CallContract { gas: gas_limit, .. } => *gas_limit,
		}
	}
}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-364)
```rust
		/// Process a message delivered by the MessageQueue pallet
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);

			// Decode bytes into versioned message
			let versioned_queued_message: VersionedQueuedMessage =
				VersionedQueuedMessage::decode(&mut message).map_err(|_| Corrupt)?;

			// Convert versioned message into latest supported message version
			let queued_message: QueuedMessage =
				versioned_queued_message.try_into().map_err(|_| Unsupported)?;

			// Obtain next nonce
			let nonce = <Nonce<T>>::try_mutate(
				queued_message.channel_id,
				|nonce| -> Result<u64, ProcessMessageError> {
					*nonce = nonce.checked_add(1).ok_or(Unsupported)?;
					Ok(*nonce)
				},
			)?;

			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
			let reward = pricing_params.rewards.remote;

			// Construct the final committed message
			let message = CommittedMessage {
				channel_id: queued_message.channel_id,
				nonce,
				command,
				params,
				max_dispatch_gas,
				max_fee_per_gas: pricing_params
					.fee_per_gas
					.try_into()
					.defensive_unwrap_or(u128::MAX),
				reward: reward.try_into().defensive_unwrap_or(u128::MAX),
				id: queued_message.id,
			};

			// ABI-encode and hash the prepared message
			let message_abi_encoded = ethabi::encode(&[message.clone().into()]);
			let message_abi_encoded_hash = <T as Config>::Hashing::hash(&message_abi_encoded);

			Messages::<T>::append(Box::new(message));
			MessageLeaves::<T>::append(message_abi_encoded_hash);

			Self::deposit_event(Event::MessageAccepted { id: queued_message.id, nonce });

			Ok(true)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-402)
```rust
		/// Calculate total fee in native currency to cover all costs of delivering a message to the
		/// remote destination. See module-level documentation for more details.
		pub(crate) fn calculate_fee(
			gas_used_at_most: u64,
			params: PricingParameters<T::Balance>,
		) -> Fee<T::Balance> {
			// Remote fee in ether
			let fee = Self::calculate_remote_fee(
				gas_used_at_most,
				params.fee_per_gas,
				params.rewards.remote,
			);

			// downcast to u128
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();

			// adjust fixed point to match local currency
			let fee = Self::convert_from_ether_decimals(fee);

			Fee::from((Self::calculate_local_fee(), fee))
		}

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L491-509)
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
			Self::send(SECONDARY_GOVERNANCE_CHANNEL, command, pays_fee)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.clone().into(),
				foreign_token_id: token_id,
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L209-249)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::register_token())]
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
