## Title
Unrecoverable panic on zero `exchange_rate` in Snowbridge outbound fee calculation causes DoS in message queue processing - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The external report describes a Chainlink price-feed dependency (`getLatestData`) that can be blocked/reverted, and because the caller has no fallback (`try`/`catch`) logic, this causes a denial of service. The direct analog in this repository is `snowbridge_pallet_outbound_queue::Pallet::calculate_fee`, which divides by a governance-supplied "price" value (`exchange_rate`) using a bare `.expect("exchange rate is not zero; qed")` instead of a checked/fallback path. This function is invoked from the public message-send entrypoint `SendMessage::validate`, so if the pricing value is ever zero, any user attempting to route an XCM message to Ethereum will trigger a panic, halting message queue delivery.

### Finding Description
`Pallet::calculate_fee` computes the remote (Ethereum) delivery fee and converts it to local currency using the governance-configured `PricingParameters.exchange_rate`: [1](#0-0) 

The division is guarded only by `.checked_div(&params.exchange_rate).expect("exchange rate is not zero; qed")` — an unconditional panic path with no `Result`/fallback handling, functionally identical to the reported oracle pattern where a blocked/zero data source is not defensively handled.

This function is reachable from an unprivileged, public entrypoint. Every outbound XCM message routed to Ethereum (via the `EthereumBlobExporter`, which any user can trigger by sending XCM/asset transfers to Ethereum from any parachain) calls `SendMessage::validate`, which unconditionally calls `calculate_fee`: [2](#0-1) 

`PricingParameters` (the "exchange rate" oracle equivalent here) is normally set and validated (non-zero) via the `set_pricing_parameters` governance extrinsic in `snowbridge-pallet-system`, using `PricingParameters::validate()`: [3](#0-2) 

However, that validation is only enforced on the governance path that explicitly sets the value. It is not enforced at the point of use in `calculate_fee`/`send_message_impl`, i.e. there is no defensive fallback if the stored/config value is ever zero (e.g., before governance first initializes it on a new deployment, if a storage migration resets it, or if the `Get<PricingParameters<...>>` implementation used by a given runtime does not itself guarantee non-zero — the trait bound is simply `Get<PricingParameters<Self::Balance>>` with no invariant enforced by the outbound-queue pallet). This mirrors exactly the reported bug class: the code assumes an external/administered value will always be in a safe state and has no catch/fallback logic for the case where it isn't.

### Impact Explanation
A panic inside `SendMessage::validate`/`calculate_fee`, which executes during normal transaction dispatch (e.g., processing an XCM `ExportMessage`/asset transfer to Ethereum), aborts execution of that extrinsic and, depending on the FRAME/Executive panic handling, can disrupt block execution for the enclosing transaction or halt the outbound queue's ability to accept new messages. Because this path is reachable by any user initiating a bridge transfer, and the outbound message queue is the sole channel for Polkadot→Ethereum message delivery (aside from governance's own primary channel, which uses the same fee function and is thus not itself protected), a zero exchange-rate state stalls bridge processing entirely — consistent with the "public underpriced work that ... stalls bridge processing" impact category.

### Likelihood Explanation
Likelihood is contingent on `PricingParameters.exchange_rate` reaching zero at the point `calculate_fee` is invoked. This is not attacker-triggerable directly (setting parameters requires governance), but the code path has no runtime safety net to prevent a panic in the event the invariant is violated (fresh deployment before initialization, migration bug, or any future runtime wiring that does not enforce `validate()` before exposing the value via `Get`). Given the current design relies solely on the `set_pricing_parameters` extrinsic to enforce non-zero, and the consuming code performs no defensive check itself, this is a structurally fragile single point of failure for the entire outbound bridge queue.

### Recommendation
Replace the `.expect(...)` panic in `calculate_fee` with a checked, gracefully-erroring path (e.g., return a `DispatchError`/`SendError` from `calculate_fee`/`validate` when `exchange_rate` is zero) instead of assuming the invariant always holds. Additionally, enforce `PricingParameters::validate()` at every point where the value is read for fee computation (defense in depth), not only at the point it is written via governance, so a zero/invalid value can never propagate into an unguarded division.

### Proof of Concept
1. Deploy or reconfigure a runtime using `snowbridge_pallet_outbound_queue` where the `PricingParameters` `Get` implementation currently resolves to a value with `exchange_rate = FixedU128::zero()` (e.g., a fresh chain before the first `set_pricing_parameters` call, or a storage/migration path that leaves the value at its `Default`, which for `FixedU128` is zero).
2. Any user submits a normal XCM transfer that routes through the `EthereumBlobExporter`, causing `SendMessage::validate` to be called.
3. `calculate_fee` executes `params.multiplier` divided-through-`checked_div(&params.exchange_rate)`, hits `None`, and the `.expect("exchange rate is not zero; qed")` panics, aborting the enclosing extrinsic/block execution and preventing further Ethereum-bound message acceptance until governance intervenes. [4](#0-3)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L379-393)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-60)
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
```

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L39-56)
```rust
	pub fn validate(&self) -> Result<(), InvalidPricingParameters> {
		if self.exchange_rate == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.fee_per_gas == U256::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.local.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.remote.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.multiplier == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		Ok(())
	}
```
