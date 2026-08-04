### Title
Outbound queue fee calculation can silently round the entire Ethereum-side relayer reward/gas fee to zero, allowing underpriced message delivery to Snowbridge - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The `RdpxV2Core`/`PerpetualAtlanticVault` bug is caused by a rounding function whose granularity (`1e6`) is large relative to the value being rounded, so a legitimate, well-formed strike price collapses to an incorrect boundary value that breaks a core economic invariant (25% OTM pricing). The Snowbridge outbound-queue pallet has the same class of bug in `Pallet::<T>::calculate_fee` / `convert_from_ether_decimals`: an integer division by a decimals-adjustment denominator can floor a valid, non-zero remote fee (which funds the relayer's gas refund and reward on Ethereum) down to exactly `0`, even though all `PricingParameters` passed `validate()` as non-zero.

### Finding Description
`calculate_fee` computes the remote (Ethereum-side) fee component that is supposed to cover gas refund + relayer reward for delivering a message to Ethereum: [1](#0-0) 

The final step, `convert_from_ether_decimals`, converts the 18-decimal wei-denominated fee into the chain's native decimals (e.g. 10 for DOT) via a plain integer division: [2](#0-1) 

`denom = 10^(18 - T::Decimals)` is a large fixed granularity (`1e8` for a 10-decimal chain). Any computed `fee` value smaller than `denom` is floored to `0` by `checked_div`, exactly analogous to how `roundUp`'s `1e6` granularity in the external report forced a non-trivial strike price up to an incorrect value. Here the direction is different (floor vs. ceiling) but the root cause is identical: a fixed rounding/scaling granularity that is too coarse relative to the magnitude of legitimately computed values, so the result silently snaps to a boundary that no longer reflects the intended economics.

This is confirmed directly by the pallet's own test, which documents the exact failure with non-zero, `validate()`-passing pricing parameters: [3](#0-2) 

`PricingParameters::validate()` only checks that inputs (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) are individually non-zero — it never checks that the resulting *computed* fee after all multiplications, divisions, and the final decimals conversion is non-zero: [4](#0-3) 

So a governance-set (or even honestly-adjusted) set of pricing parameters that are individually valid can still yield `fee.remote == 0` for messages whose `max_dispatch_gas` is small, because `fee_per_gas * gas_used_at_most + reward` may not exceed `denom` after the multiplier/exchange-rate and decimals scaling.

### Impact Explanation
`fee.remote` is the amount that the pallet is supposed to require the sender (`SendMessage::validate`) to pay in order to cover the relayer's gas refund and reward for delivering the message to Ethereum: [5](#0-4) 

If this component computes to `0`, the message is still accepted and enqueued (`deliver` has no check on `fee.remote`), but the upstream fee charged to the user for the remote (Ethereum) component is zero. This is "public underpriced work" for the outbound bridge channel: messages can be dispatched to Ethereum without covering relayer gas/reward costs, which degrades relayer incentives and can stall processing of the outbound queue to Ethereum (relayers have no incentive to deliver messages with zero attached reward/gas refund), matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
This requires no malicious actor, admin abuse, or privileged action beyond ordinary configuration of `PricingParameters` (which is expected to be routinely updated by governance per the module's own docs) and ordinary, permissionless use of `SendMessage::validate`/`deliver` by any pallet that sends messages through the bridge (e.g. XCM export). Any combination of small `gas_used_at_most`, low `fee_per_gas`/`reward`, or an exchange rate/multiplier combination that pushes the pre-conversion fee below the `10^8` (or similar) floor triggers the bug deterministically — this is a straightforward arithmetic property, not a probabilistic or attacker-controlled edge case, and is already reproduced by the repository's own unit test.

### Recommendation
- In `convert_from_ether_decimals`/`calculate_fee`, round the remote fee **up** (ceiling division) rather than truncating, and/or enforce a post-computation floor so `fee.remote` can never be zero when the underlying wei-denominated fee is non-zero.
- Extend `PricingParameters::validate` (or add a runtime check in `calculate_fee`) to reject/flag configurations where the computed remote fee for the minimum supported `gas_used_at_most` would round to zero, rather than only checking that the raw input fields are individually non-zero.
- Add a defensive assertion/error path (e.g. return `SendError` from `validate`) if `fee.remote == 0` while `pricing_params.rewards.remote != 0`, so under-priced messages cannot enter the outbound queue silently.

### Proof of Concept
The existing repository test already demonstrates the exact defect with realistic, individually-valid parameters:
```
exchange_rate = 1/1, fee_per_gas = 1, rewards = {local:1, remote:1}, multiplier = 1/1
gas_used = 250_000

remote_fee_wei = fee_per_gas * gas_used + reward = 250_000 * 1 + 1 = 250_001
after FixedU128 multiplier/exchange_rate scaling => 250_001 (unchanged, since both are 1)
convert_from_ether_decimals: denom = 10^(18 - Decimals)
=> 250_001 / denom == 0  (floors to zero)

assert_eq!(fee.remote, 0);   // pallet's own test explicitly flags this as invalid
``` [3](#0-2) 

This message would still be accepted by `SendMessage::validate`/`deliver` and enqueued for delivery to Ethereum with `fee.remote == 0`, i.e., no relayer reward/gas-refund fee collected for the remote leg of the bridge.

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
