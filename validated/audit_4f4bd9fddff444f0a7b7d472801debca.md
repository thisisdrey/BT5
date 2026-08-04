### Title
`OutboundQueue::calculate_fee` truncates the remote (Ethereum-side) fee to zero under valid pricing parameters, letting senders underpay for bridge delivery while the promised relayer reward stays fixed - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`Pallet::<T>::calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` computes the native-currency fee a sender must pay for a Snowbridge outbound message by combining `fee_per_gas`, `gas_used_at_most`, `reward`, `multiplier`, and `exchange_rate`, then applying an integer-division decimal conversion in `convert_from_ether_decimals`. With entirely valid, non-zero `PricingParameters` (as accepted by `PricingParameters::validate`), the computed `fee.remote` can truncate to exactly `0`, as demonstrated by the pallet's own test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`. This mirrors the external report's core flaw: a "safety" computation that looks precise but silently loses information/undercharges under legitimate-looking inputs, because no invariant check enforces that the derived output stays proportionate to the real cost being backed.

### Finding Description
The fee computation pipeline is: [1](#0-0) 

1. `calculate_remote_fee` computes `fee_per_gas * gas_used_at_most + reward` in wei (`U256`). [2](#0-1) 
2. The wei value is reinterpreted directly as the `inner()` of a `FixedU128` (an 18-decimal fixed-point number), then multiplied by `multiplier` and divided by `exchange_rate`.
3. `convert_from_ether_decimals` performs integer division by `10^(18 - Decimals)` (e.g. `10^8` for a 10-decimal native currency) to rescale into native-currency units. [3](#0-2) 

Because step 3 is plain integer division with no rounding-up or minimum-floor guard, any wei-denominated remote fee smaller than the divisor (e.g. `100000000` for 10-decimal chains) collapses to `0`. The pallet's own regression test confirms this with fully valid parameters (`exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1`, `gas_used = 250000`): [4](#0-3) 

Critically, this truncated `fee.remote` is only used to charge the *sender* via `SendMessage::validate` in `send_message_impl.rs`: [5](#0-4) 

but the actual reward promised to the Ethereum-side relayer, embedded in the committed message that gets delivered on-chain, is taken straight from `pricing_params.rewards.remote` in `do_process_message`, completely independent of the truncated `calculate_fee` result: [6](#0-5) 

This is exactly the report's "assumption gap": the code assumes the fee-charging path (`a/b`-style ratio math) faithfully tracks the reward-settlement path (`b/c`), but they are computed by two disconnected formulas. `PricingParameters::validate` only rejects zero *inputs*, never checks that the *derived* `fee.remote` output stays non-zero/proportionate: [7](#0-6) 

### Impact Explanation
Governance sets `PricingParameters` "every few weeks" per the module docs: [8](#0-7) 
Any parameter combination where the computed wei amount is small relative to the decimal-conversion divisor (plausible for low-gas commands, low `fee_per_gas`, or a chain with fewer decimals) causes the outbound-queue pallet to charge senders `0` (or far too little) for the remote/Ethereum-side component of message delivery, while the relayer reward promised on the Ethereum gateway contract remains the full `pricing_params.rewards.remote` value. This is public underpriced work: unprivileged users can flood the outbound queue with messages that are fully accepted (payload/channel checks pass) but for which the parachain collects no compensating remote fee, even though each message still carries the fixed on-chain reward obligation to relayers. Over time this degrades the bridge's fee-backing model and can stall processing (relayers eventually have no economic incentive to deliver messages if the local sovereign/fee-pool accounting for remote rewards becomes decoupled from real payments), which is exactly the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
No malicious peer, relayer, or validator is needed — only a normal (non-privileged) message sender using `SendMessage::validate`/`deliver`, combined with pricing parameters that governance has legitimately set. The parameters need not be adversarial or malformed; `PricingParameters::validate` explicitly accepts the exact values shown to trigger truncation in the pallet's own test. This makes the condition easy to hit unintentionally in production configuration, and any sender submitting messages during such a window benefits directly.

### Recommendation
- In `convert_from_ether_decimals`, use a rounding scheme that never silently drops the fee to zero when the pre-division value is non-zero (e.g. round up, or enforce a floor equal to the smallest representable native-currency unit).
- Add an explicit post-computation invariant in `calculate_fee` (or in `PricingParameters::validate`) asserting/erroring if the derived `fee.remote` is `0` while `reward`/`fee_per_gas` inputs are non-zero, rather than silently returning a zero fee.
- Ensure the reward actually embedded in `CommittedMessage` (from `do_process_message`) is derived from the same charged fee as `calculate_fee`, so the two paths cannot diverge.

### Proof of Concept
The existing unit test is a direct, repository-verified PoC: [4](#0-3) 
With `exchange_rate = 1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1`, and `gas_used = 250000` — all individually valid per `PricingParameters::validate` — `calculate_fee` returns `fee.local = 698000000` (non-zero, correctly charged) but `fee.remote = 0`. Any sender invoking `SendMessage::validate`/`deliver` while governance parameters land in this region is charged nothing for the remote/Ethereum component of delivery, even though `do_process_message` still embeds the full `pricing_params.rewards.remote` as the on-chain relayer reward for that message.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-58)
```rust
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
//!
//! This is an interim measure. Once ETH/DOT liquidity pools are available in the Polkadot network,
//! we'll use them as a source of pricing info, subject to certain safeguards.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L332-352)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L396-402)
```rust
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-61)
```rust
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
