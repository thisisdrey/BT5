Audit Report

## Title
`Fee(Message)` charged from users can round its Ethereum-side `remote` component to zero, causing Snowbridge relayers to deliver messages unrewarded and letting bridge processing stall - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

## Summary
`pallet_outbound_queue::Pallet::calculate_fee` computes the DOT-denominated fee for delivering an XCM message to Ethereum, splitting it into `local` and `remote` (relayer gas-refund/reward) components. The `remote` component passes through `convert_from_ether_decimals`, a plain integer division by `10^(18-Decimals)`, with no minimum-fee floor, allowing plausible non-zero governance-set pricing parameters to floor it to `0` while the message is still accepted and queued for Ethereum delivery.

## Finding Description
`calculate_fee` at [1](#0-0)  computes the remote fee in wei via `calculate_remote_fee`, scales by `multiplier`/`exchange_rate` using `FixedU128`, then truncates to native decimals via `convert_from_ether_decimals`, defined at [2](#0-1) , which performs `value.checked_div(denom)` with no floor or non-zero enforcement.

`PricingParameters::validate()` at [3](#0-2)  only checks that `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` are individually non-zero — it never simulates the actual `calculate_fee` derivation, so parameter sets that pass validation can still yield a zero derived `remote` fee for realistic `gas_used_at_most` values.

`SendMessage::validate` at [4](#0-3)  computes `fee` unconditionally and returns `Ok((ticket, fee))` at line 73 without checking whether `fee.remote` is zero. `deliver` at lines 76-88 of the same file unconditionally enqueues the message via `T::MessageQueue::enqueue_message`, regardless of the fee outcome. The corrupted value is the `remote` component of the `Fee<T::Balance>` struct returned from `calculate_fee`/`validate`, which is used downstream to compute the on-chain `reward` field of the `CommittedMessage` sent to Ethereum (see `reward = pricing_params.rewards.remote` and its use in the committed message at [5](#0-4) ).

## Impact Explanation
This matches the allowed impact "public underpriced work that ... stalls bridge processing": messages can be committed and merkle-rooted for Ethereum delivery while the DOT fee charged to the user includes a `remote` component of zero, meaning the relayer executing the corresponding gas expenditure and reward payout on the Ethereum gateway receives nothing. This directly undermines the fee model documented in the module docs (`Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward`), degrading relayer incentives and potentially stalling outbound message processing to Ethereum.

## Likelihood Explanation
The pallet's own test, `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` at [6](#0-5) , demonstrates with fully valid (individually non-zero) pricing parameters that `OutboundQueue::calculate_fee(250_000, price_params)` yields `fee.remote == 0`. Because `exchange_rate`, `fee_per_gas`, and `multiplier` are governance-settable and Ethereum gas prices/ETH-DOT rates are volatile, this zero-fee condition is reachable through normal parameter drift and is triggerable by any user submitting a message when parameters land near this boundary, without requiring malicious relayer or validator behavior.

## Recommendation
- In `calculate_fee`, enforce a minimum non-zero `remote` fee (analogous to `min_converted_fee` in `pallet-transaction-payment` asset adapters) whenever the pre-truncation remote fee was non-zero.
- Extend `PricingParameters::validate` or add an integrity check that simulates `calculate_fee` for a representative `gas_used_at_most` and rejects parameter sets producing a zero `remote` component.
- In `SendMessage::validate`, explicitly reject tickets whose `fee.remote` is zero instead of silently accepting and queuing the message.

## Proof of Concept
The existing repository test at [6](#0-5)  reproduces the bug directly: with `exchange_rate = FixedU128::from_rational(1,1)`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = FixedU128::from_rational(1,1)` (all individually valid per `validate()`), `calculate_fee(250_000, price_params)` returns `fee.local = 698000000` and `fee.remote = 0`. Feeding this `Fee` through `SendMessage::validate`/`deliver` in `send_message_impl.rs` would enqueue and commit the message for Ethereum delivery with zero relayer compensation, since neither function checks `fee.remote` for zero before proceeding.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L337-352)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
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
