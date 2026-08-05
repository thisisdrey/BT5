### Title
Unchecked division by `PricingParameters.exchange_rate` in `calculate_fee` panics the public message-send path, stalling all Snowbridge outbound delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Chainlink report's core broken invariant is: a price/rate value that a critical path depends on can become unusable (revert/blocked) with **no fallback**, and the code assumed the value is always safe to consume, turning a routine read into a denial-of-service across every function on that path. The same "assume rate is always valid, no fallback on failure" pattern exists in Snowbridge's outbound-queue fee computation, where `PricingParameters.exchange_rate` is divided into without any runtime safety net other than an `.expect()` that will panic instead of gracefully failing.

### Finding Description
`Pallet::calculate_fee` computes the DOT-denominated delivery fee for every outbound message to Ethereum: [1](#0-0) 

The critical line is:
```rust
.checked_div(&params.exchange_rate)
.expect("exchange rate is not zero; qed")
```
This is invoked from `SendMessage::validate`, the **public entry point** that runs on every single XCM message routed to Ethereum through the bridge (e.g. any user's asset transfer/XCM export via `EthereumBlobExporter`/outbound-queue pipeline): [2](#0-1) 

`params` is read fresh from storage via `T::PricingParameters::get()` on every call with no re-validation at read time. `PricingParameters::validate()` exists and rejects a zero `exchange_rate`, but it is only invoked (per repo evidence in `snowbridge-pallet-system`) at the moment governance calls `set_pricing_parameters`; there is no invariant enforcement on the storage value itself at every read, and other code in the very same function already anticipates unsafe/invalid inputs by using `defensive_unwrap_or(u128::MAX)` for the `fee` and `reward` casts just lines above: [3](#0-2) 

This inconsistency — defensive handling for some inputs, a hard `.expect()` panic for the exchange rate — mirrors exactly the audited bug class: a single upstream "price" value with no fallback path turns every dependent function (`viewPrice`/`getPrice`, and their callers `getCollateralValueInternal`/`getWithdrawalLimitInternal` in the Chainlink report) into a denial-of-service point. Here, the affected functions are `calculate_fee` and its caller `SendMessage::validate`, which gates **all** outbound Snowbridge traffic (normal user messages and, depending on channel, everything except the primary governance channel already enqueued).

### Impact Explanation
If `exchange_rate` in the on-chain `PricingParameters` storage is ever zero — whether via a migration bug, uninitialized/default genesis value, or an unvalidated governance write path that bypasses `validate()` — every call to `SendMessage::validate` will panic instead of returning a graceful `Err(SendError)`. Since this function is on the hot path for constructing outbound XCM/bridge messages, a panic here degrades or halts bridge message processing for **all** users attempting to send messages to Ethereum, which matches the required impact class "public underpriced work that degrades block production or stalls bridge processing." Unlike a `Result`-based rejection, a Rust panic in this context is a defensive-programming failure (`.expect()` instead of `checked_div`-based graceful error), removing any recoverable state and creating a hard failure surface no ordinary user can work around.

### Likelihood Explanation
Likelihood depends entirely on `exchange_rate` reaching a zero value in storage without triggering `validate()`. Repository evidence confirms `validate()` is enforced only within the `set_pricing_parameters` extrinsic itself, not as a storage-level invariant re-checked on every read; this leaves migrations, defaults, or any future code path that writes `PricingParameters` directly to storage unguarded. This is analogous to the "fixed price not mandatory, no fallback if feed blocked" design gap called out in the external report. I could not fully verify from the indexed excerpts whether every current write path (including `snowbridge-pallet-system::migration`) unconditionally revalidates before storing — this would need to be confirmed with full file access to `bridges/snowbridge/pallets/system/src/migration.rs` and `lib.rs`.

### Recommendation
Replace `.expect("exchange rate is not zero; qed")` with a checked/graceful path: return `SendError`/a defensive fallback fee (or reject the message safely) when `checked_div` yields `None`, consistent with the `defensive_unwrap_or` pattern already used elsewhere in the same function. Additionally, enforce `PricingParameters::validate()` as an invariant at every write path (including migrations) rather than relying solely on extrinsic-level validation, so a zero exchange rate can never reach `calculate_fee`.

### Proof of Concept
1. Suppose `PricingParameters.exchange_rate` becomes `FixedU128::zero()` in storage (e.g., via a migration that writes a default/placeholder value without calling `validate()`, or an oversight in a future upgrade path).
2. Any user submits an XCM message that gets exported to Ethereum via the Snowbridge outbound queue (e.g., a reserve asset transfer to Ethereum).
3. `EthereumBlobExporter`/outbound-queue invokes `Pallet::<T>::validate(&message)` → `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())`.
4. Inside `calculate_fee`, `checked_div(&params.exchange_rate)` returns `None` because `exchange_rate == 0`, and `.expect("exchange rate is not zero; qed")` panics.
5. Every subsequent call to `validate` for any user's Ethereum-bound message panics identically, since nothing resets or bypasses the corrupted `exchange_rate` value — mirroring the original report's DOS pattern where a single unguarded price dependency caused system-wide function failure with no fallback logic in place. [4](#0-3) [5](#0-4)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-393)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-74)
```rust
	fn validate(
		message: &Message,
	) -> Result<(Self::Ticket, Fee<<Self as SendMessageFeeProvider>::Balance>), SendError> {
		// The inner payload should not be too large
		let payload = message.command.abi_encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		// Ensure there is a registered channel we can transmit this message on
		ensure!(T::Channels::contains(&message.channel_id), SendError::InvalidChannel);

		// Generate a unique message id unless one is provided
		let message_id: H256 = message
			.id
			.unwrap_or_else(|| unique((message.channel_id, &message.command)).into());

		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

		let queued_message: VersionedQueuedMessage = QueuedMessage {
			id: message_id,
			channel_id: message.channel_id,
			command: message.command.clone(),
		}
		.into();
		// The whole message should not be too large
		let encoded = queued_message.encode().try_into().map_err(|_| SendError::MessageTooLarge)?;

		let ticket = Ticket { message_id, channel_id: message.channel_id, message: encoded };

		Ok((ticket, fee))
	}
```
