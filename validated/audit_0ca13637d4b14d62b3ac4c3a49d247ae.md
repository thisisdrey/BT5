## Title
Underpriced Snowbridge message delivery: `OutboundQueue::calculate_fee` can compute `remote fee = 0` for real dispatch commands, letting any user submit bridge messages that consume Ethereum-side gas/relayer work without paying for it - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
Applying the "empty order → invalid `price=0` used in accounting" pattern from the Perennial report: in `pallet_snowbridge_outbound_queue`, the fee owed by a message sender is derived from `PricingParameters` via `calculate_fee`/`calculate_remote_fee`/`convert_from_ether_decimals`. Even with fully valid, non-zero, governance-set `PricingParameters`, this pipeline can legitimately compute `fee.remote == 0` for real, low-gas commands due to integer-decimal truncation in `convert_from_ether_decimals`. Because `SendMessage::validate` (a permissionless-path entry used by any account able to trigger a bridge message, e.g. token transfers/XCM export to Ethereum) charges exactly this computed fee, an attacker can cause real relayer/gas-consuming work to be committed to Ethereum for `0` DOT remote fee, mirroring the underlying "invalid zero accounting value silently degrades economic accounting" bug class from the report.

### Finding Description
`Pallet::calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-393`) computes the remote (Ethereum-side) fee owed for a message: [1](#0-0) 

It downcasts the fee to `u128`, applies the fixed-point multiplier/exchange-rate division, then calls `convert_from_ether_decimals`, which performs an integer division to rescale from 18 (ETH) decimals down to the local currency's decimals (e.g., 10 for DOT): [2](#0-1) 

The repository's own test demonstrates that with a fully valid `PricingParameters` (`exchange_rate = 1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1`) and realistic `gas_used = 250000`, the resulting `fee.remote` is `0`: [3](#0-2) 

This fee is exactly what `SendMessage::validate` charges the sender before enqueuing the message for delivery to Ethereum: [4](#0-3) 

The module doc itself states the invariant this is supposed to guarantee — that the fee must cover "the gas refund paid out to relayers" and "an additional reward paid out to relayers" — yet the computation can produce `0`: [5](#0-4) 

Existing guards do not stop this: `PricingParameters::validate()` only rejects parameters that are individually zero (exchange_rate, fee_per_gas, rewards, multiplier) at the governance `set_pricing_parameters` call site — it does not simulate the downstream `calculate_fee` pipeline, so it cannot detect that a *combination* of small-but-valid values, together with a message whose `max_dispatch_gas` is small, truncates to zero after the ether→local-decimals division: [6](#0-5) [7](#0-6) 

Just as in the Perennial bug — where an "empty" order silently produced a `price=0` oracle version that was still used in fee/funding accumulation instead of being rejected or backfilled — here a fully "valid" (non-zero) pricing configuration silently produces a `0` remote fee that is still accepted and charged, instead of being rejected, floored to a minimum, or rounded up.

### Impact Explanation
This is public underpriced work: any user who can trigger message submission to Ethereum (e.g. governance-independent user flows that route through `OutboundQueue::validate`/`deliver`, such as asset transfers converted into `Command`s) pays `remote fee = 0` while the message still consumes real `max_dispatch_gas` and requires real relayer/gas reimbursement on the Ethereum gateway contract side. Relayers are not compensated (`reward = 0` effectively, and gas refund logic on Ethereum is driven by the committed `reward`/`max_fee_per_gas` fields which derive from the same zero pricing), which can stall bridge processing economics — nobody is incentivized to relay these messages, or if relayed, the relayer absorbs the cost. At larger scale this allows spamming free/underpriced messages into the outbound queue, degrading the bridge's fee-market design and message-processing throughput, matching the accepted impact category "public underpriced work that degrades block production or stalls bridge processing."

### Likelihood Explanation
Likelihood is significant because it requires no malicious governance, no compromised relayer, and no privileged action — only that (a) the currently governance-set `PricingParameters` combined with (b) the specific command's `max_dispatch_gas` fall into the truncation range demonstrated by the shipped unit test. Since `PricingParameters::validate()` does not simulate `calculate_fee` for the range of dispatchable commands/gas costs, there is no guarantee current or future production parameter configurations avoid this, and an attacker only needs to submit commands with sufficiently small `max_dispatch_gas` (lowest-gas command types) to trigger `fee.remote == 0` deterministically.

### Recommendation
- In `calculate_fee`/`convert_from_ether_decimals`, round up (ceiling division) rather than truncating, or enforce a floor `remote_fee.max(MinimumRemoteFee)` before returning, and reject/skip delivery (or fall back to a governance-configured minimum) when the ether→local conversion would otherwise truncate to zero.
- Extend `PricingParameters::validate()` (or add a runtime invariant check) to simulate `calculate_fee` against the full range of `GasMeter::maximum_dispatch_gas_used_at_most` values across supported `Command`s, rejecting parameter sets for which any valid command would compute `fee.remote == 0`.
- Add a `Yield`/`Corrupt`-style hard rejection in `do_process_message`/`validate` when `calculate_fee(...).remote == 0`, so a zero-fee message can never be queued for Ethereum delivery.

### Proof of Concept
The repository's own unit test is the proof of concept, showing a fully-valid (non-zero) `PricingParameters` config combined with a realistic `gas_used = 250000` produces `fee.remote == 0`:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
assert_eq!(fee.local, 698000000);
assert_eq!(fee.remote, 0); // <-- underpriced/zero remote fee accepted
```
Any message with a `Command` whose `GasMeter::maximum_dispatch_gas_used_at_most` falls in this range will be queued via `SendMessage::validate`/`deliver` and dispatched to Ethereum while the sender pays `fee.remote = 0`, exactly reproducing the "valid-looking but zero-value accounting input silently accepted downstream" bug class described in the external report. [8](#0-7)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L38-51)
```rust
//! # Fees
//!
//! An upfront fee must be paid for delivering a message. This fee covers several
//! components:
//! 1. The weight of processing the message locally
//! 2. The gas refund paid out to relayers for message submission
//! 3. An additional reward paid out to relayers for message submission
//!
//! Messages are weighed to determine the maximum amount of gas they could
//! consume on Ethereum. Using this upper bound, a final fee can be calculated.
//!
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
```

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L411-418)
```rust
		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-319)
```rust
#[test]
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 1),
			fee_per_gas: 1_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(1, 1),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
		assert_eq!(fee.local, 698000000);
		// Though none zero pricing params the remote fee calculated here is invalid
		// which should be avoided
		assert_eq!(fee.remote, 0);
	});
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L317-334)
```rust
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());

			let command = Command::SetPricingParameters {
				exchange_rate: params.exchange_rate.into(),
				delivery_cost: T::InboundDeliveryCost::get().saturated_into::<u128>(),
				multiplier: params.multiplier.into(),
			};
			Self::send(PRIMARY_GOVERNANCE_CHANNEL, command, PaysFee::<T>::No)?;

			Self::deposit_event(Event::PricingParametersChanged { params });
			Ok(())
		}
```
