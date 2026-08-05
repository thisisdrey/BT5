### Title
Snowbridge outbound-queue `calculate_fee` can round the remote (Ethereum-side) relayer fee to zero, letting governance-set but low-magnitude pricing parameters produce free/underpriced message delivery - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`Pallet::<T>::calculate_fee` in the Snowbridge outbound-queue pallet computes the DOT-denominated fee that a user must pay via `SendMessage::validate` before a message is enqueued for relay to Ethereum. The remote-fee leg of this computation misuses `FixedU128::from_inner` on a raw wei value and then truncates via integer division in `convert_from_ether_decimals`, so for pricing parameters that are individually non-zero but numerically small (e.g. `exchange_rate = 1`, `fee_per_gas = 1`), the resulting DOT fee for the remote/relayer component rounds down to `0`. This is confirmed by the pallet's own test [1](#0-0) , which explicitly documents `fee.remote == 0` for "valid"/non-zero pricing parameters and even comments "which should be avoided."

### Finding Description
`calculate_fee` computes the remote fee in wei via `calculate_remote_fee` (`fee_per_gas * gas_used_at_most + reward`), then does: [2](#0-1) 

The critical bug is the line `let fee = FixedU128::from_inner(fee)...`: `FixedU128::from_inner` interprets the raw integer as the fixed-point *internal* representation (i.e. `value * 10^-18`), effectively dividing the wei fee by `10^18` before any of the intended decimal conversion happens. `convert_from_ether_decimals` then does an *integer* division by `10^(18 - Decimals)` (e.g. `10^8` for a 10-decimal chain) with `checked_div`, silently floor-truncating any resulting fractional DOT amount to `0`: [3](#0-2) 

Because `PricingParameters::validate()` only rejects *exactly-zero* fields (`exchange_rate == 0`, `fee_per_gas == 0`, `rewards == 0`, `multiplier == 0`) and never enforces a minimum magnitude relative to `gas_used_at_most` and decimal scaling, governance can set (or already has set, in test fixtures) parameters that pass validation yet still yield a `fee.remote` of `0` DOT for real messages: [4](#0-3) 

This fee is exactly what `SendMessage::validate` returns to be charged before a message is queued for on-chain commitment and relay to Ethereum: [5](#0-4) 

There is no secondary check anywhere in `do_process_message`, `commit`, or the calling XCM exporter that re-validates the computed fee is non-trivial before the message is queued, committed into the merkle root, and made available for relayers.

### Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" impact category. If `fee.remote` computes to `0`, users pay only the local weight-fee component while the remote relayer reward/gas-refund component is entirely unbacked. This breaks the core invariant documented in the pallet's own header comment (`RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))`) that every remote gas/reward cost must be paid for upfront. In production this can lead to: (a) relayers no longer being economically incentivized to submit messages that were queued with zero remote reward, causing bridge message backlog/stalling, or (b) if any subsidized/treasury top-up mechanism exists to cover unpaid remote fees, an unbacked drain of DOT reserves for messages that never paid their real cost - both are within the accepted "underpriced work" / "bridge-state" impact classes.

### Likelihood Explanation
The bug is reachable purely through legitimate, already-passing `validate()` pricing parameters — no malicious relayer, prover, or admin abuse is required to *trigger* the arithmetic bug once parameters are configured; the flaw is in the calculation itself, and the pallet's own unit test demonstrates the exact zero-fee outcome with parameter values that are far from pathological (`exchange_rate=1`, `fee_per_gas=1`, `reward=1`). Any low-decimal-precision native currency combined with governance-set pricing near these magnitudes will reproduce the zero-fee outcome for ordinary message sizes.

### Recommendation
Fix the unit conversion in `calculate_fee`: convert the wei-denominated remote fee to the native currency using consistent explicit scaling (multiply/divide by `10^18` and `10^Decimals` using checked arithmetic) rather than abusing `FixedU128::from_inner`, and add an explicit check (rather than silent truncation to zero) that rejects/rounds up computed fees below a minimum floor. Additionally, extend `PricingParameters::validate()` to reject parameter combinations that, given expected message sizes/gas usage, would compute to a zero DOT-denominated remote fee, rather than only checking that individual fields are non-zero.

### Proof of Concept
The pallet's existing regression test already demonstrates the bug end-to-end: [1](#0-0) 
With `exchange_rate = FixedU128::from_rational(1,1)`, `fee_per_gas = 1`, `rewards = {local:1, remote:1}`, `multiplier = 1`, and `gas_used = 250000`, `calculate_remote_fee` returns `250001` wei, which is non-zero and would pass `PricingParameters::validate()`, yet `OutboundQueue::calculate_fee` returns `fee.remote == 0`, meaning a message can be validated and enqueued for Ethereum delivery while the relayer reward/gas-refund component charged is exactly zero.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-318)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L368-393)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L404-418)
```rust
		/// The local component of the message processing fees in native currency
		pub(crate) fn calculate_local_fee() -> T::Balance {
			T::WeightToFee::weight_to_fee(
				&T::WeightInfo::do_process_message().saturating_add(T::WeightInfo::commit_single()),
			)
		}

		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L35-56)
```rust
impl<Balance> PricingParameters<Balance>
where
	Balance: BaseArithmetic + Unsigned + Copy,
{
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
