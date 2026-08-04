Based on the investigation, the closest verifiable local analog to the Ajna "wrong price used, causing a biased/incorrect economic value" bug pattern is in Snowbridge's outbound-queue fee calculation, where a rounding/order-of-operations defect can cause the computed remote (Ethereum-side) fee to collapse to zero even though all pricing parameters are validated as non-zero.

### Title
Snowbridge outbound-queue `calculate_fee` can produce a zero remote fee/reward despite non-zero pricing parameters, enabling underpriced message dispatch - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
`Pallet::<T>::calculate_fee` computes the fee a user pays to send a message to Ethereum by combining `fee_per_gas * gas_used_at_most + reward` (in wei), converting to `u128`, then multiplying by `params.multiplier`, dividing by `params.exchange_rate`, and finally down-scaling decimals via `convert_from_ether_decimals` (integer division by `10^decimals`). [1](#0-0) 

### Finding Description
`PricingParameters::validate` only rejects the case where `exchange_rate`, `fee_per_gas`, `rewards.local/remote`, or `multiplier` are exactly zero. [2](#0-1) 
It does not guard against the *combination* of these values producing a rounded-to-zero result once run through `calculate_fee`'s chain of `FixedU128` multiplication/division followed by integer division in `convert_from_ether_decimals`. The repository's own test suite demonstrates this: with `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1/1`, the resulting `fee.remote` is `0`, even though every pricing parameter individually passed `validate()`. [3](#0-2) 
The test's own comment explicitly flags this as unwanted behavior: "Though none zero pricing params the remote fee calculated here is invalid which should be avoided." [4](#0-3) 

The root cause mirrors the Ajna BPF bug's structure: a derived value (BPF / here, the remote fee) is computed from an input (auction price / here, gas-and-reward priced in wei) without clamping/validating against the economically-meaningful floor (bucket price / here, a non-zero fee that actually compensates gas + relayer reward), so downstream consumers (bond reward accounting / here, the fee charged to the sender and the reward promised to relayers) can silently diverge from the intended value.

### Impact Explanation
If `calculate_fee`'s `remote` component rounds to zero for the currently configured `PricingParameters` (which are settable via governance/authorized origin and could reach values close to this edge unintentionally, e.g. after an exchange-rate update), messages could be accepted and committed by `do_process_message` while charging (or promising) an insufficient/zero remote fee relative to the actual Ethereum gas + relayer reward needed. This constitutes "public underpriced work that ... stalls bridge processing," since relayers have no incentive to deliver messages whose promised reward/fee does not cover real costs, causing the outbound queue to back up (`MessageLeaves` growing, `MaxMessagesPerBlock` yield-limits kicking in) without messages actually being relayed to Ethereum. [5](#0-4) 

### Likelihood Explanation
This requires no privileged/malicious actor — it depends solely on the currently configured `PricingParameters` (any legitimate governance-set exchange rate/fee-per-gas/multiplier combination) interacting with the deterministic rounding behavior of `calculate_fee`. `validate()` provides a false sense of safety because it checks only for absolute zero on each field independently, not for the composed rounding result, so a parameter update that appears valid can still produce this outcome. The bug is deterministic and reproducible (as shown by the existing test), not reliant on race conditions or specific block timing.

### Recommendation
Extend `PricingParameters::validate` (or add a check inside `calculate_fee`) to reject/round-up when the computed `fee.remote` (post `convert_from_ether_decimals`) evaluates to zero, or require a minimum non-zero remote fee floor analogous to how bucket-price flooring is recommended in the source report. Consider performing the decimal down-scaling with rounding-up (ceiling division) rather than truncating integer division so genuinely small but non-zero economic values are preserved rather than being zeroed out.

### Proof of Concept
The existing unit test in the repository already reproduces the defect deterministically: [3](#0-2) 
With `gas_used = 250000`, `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}`, `multiplier = 1/1`, calling `OutboundQueue::calculate_fee(gas_used, price_params)` yields `fee.remote == 0` while `fee.local == 698000000`, confirming that a fully "valid" (non-zero-per-field) pricing configuration can still yield a zero remote fee/reward for message dispatch.

**Caveat:** I was unable to fully trace, within the available iterations, how `calculate_fee`'s output is consumed in `send_message_impl.rs` (e.g., whether `validate()` there enforces any additional floor before allowing dispatch/commit). This chain (governance-parameter → zero fee → committed message → stalled relaying) is inferred from the pricing/fee code and the pre-existing test comment, but the end-to-end exploitability through the public `send_message`/XCM export entrypoint was not independently re-verified in this pass.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-313)
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
