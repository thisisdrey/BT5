### Title
Integer-division rounding in Snowbridge outbound-queue `calculate_fee` can silently zero out the relayer fee, allowing underpriced Ethereum-bound messages - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
The Chainlink-oracle report's core defect is: when a price/oracle-derived value degenerates (oracle failure → `0`), the contract still proceeds with the swap using that value as the slippage floor, so downstream logic accepts an economically wrong (unbacked) minimum. The local analog is in `Pallet::calculate_fee` in the Snowbridge outbound-queue pallet, where a fully valid, non-zero `PricingParameters` set can still produce a `Fee.remote` of exactly `0` due to integer-division truncation in `convert_from_ether_decimals`, and this degenerate `0` is accepted and used as the actual charge with no floor check.

### Finding Description
`Pallet::calculate_fee` computes the remote (Ethereum-side) component of a message's delivery fee: [1](#0-0) 

The remote fee in wei is computed by `calculate_remote_fee` (`fee_per_gas * gas_used_at_most + reward`), then reinterpreted via `FixedU128::from_inner(fee)` (treating the raw wei value as an 18-decimal fixed-point number), multiplied by `multiplier`, divided by `exchange_rate`, and finally passed through `convert_from_ether_decimals`, which performs an integer division by `10^(18 - Decimals)`: [2](#0-1) 

Because Rust integer division truncates, any remote-fee value smaller than the divisor (`10^8` for a 10-decimal native currency) collapses to `0`. The pallet's own test suite documents this exact scenario with non-zero, "valid" pricing parameters: [3](#0-2) 

`PricingParameters::validate()` only guards against parameters being literally zero (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`), it does not — and cannot — guard against the combination of these non-zero values producing a rounded-to-zero *result*: [4](#0-3) 

The computed `Fee` (local, remote) is used directly by `SendMessage::validate` in `send_message_impl.rs` as the amount the sender must pay before the message ticket is issued and enqueued — there is no minimum-fee floor check on the `remote` component: [5](#0-4) 

This mirrors the reported bug class precisely: a value meant to protect against economic manipulation (there, a slippage floor; here, the relayer-reward/gas-refund component of the fee) silently degrades to `0` through an unguarded arithmetic path, and the caller proceeds anyway as if the value were valid.

### Impact Explanation
If the remote fee rounds down to zero, a sender pays only the `local` fee (which covers on-chain processing weight, not Ethereum gas), while the message is still committed and relayed for execution on Ethereum. Relayers receive no gas refund and no reward for that message. This is exactly the "public underpriced work" pattern flagged in scope: an unprivileged, ordinary user can enqueue outbound Snowbridge messages whose true remote-execution cost is not paid for. At volume this discourages/starves relayers economically and can degrade or stall the bridge's outbound message processing pipeline, since committed messages accumulate on Ethereum-side backlog without adequate incentive to relay them. It does not require a malicious relayer, validator, or governance actor — only ordinary message senders and governance-set (non-malicious) pricing parameters that happen to combine into a sub-threshold remote fee.

### Likelihood Explanation
Likelihood is moderate: it requires governance-configured `PricingParameters` (via `set_pricing_parameters` in `snowbridge-pallet-system`) to fall into a numeric range where `RemoteFeeAdjusted` rounds to less than `10^(18-Decimals)` wei-equivalent — plausible for low-gas commands (`gas_used_at_most` small) combined with a low `fee_per_gas`/`reward` and a favorable exchange rate, as demonstrated directly by the existing unit test. It does not require an attacker to control the oracle/pricing parameters, only to send messages while parameters happen to sit in this degenerate band, or governance to inadvertently set such parameters (since `validate()` gives no signal that the *resulting* fee could be zero).

### Recommendation
Add a floor check to `calculate_fee` (or `convert_from_ether_decimals`) that rejects or corrects a computed `Fee.remote == 0` when `gas_used_at_most > 0` and pricing parameters are non-zero — e.g., round up (`ceil`) instead of truncating during decimal conversion, or return an error/require a minimum charge of `1` unit, so the sender can never enqueue Ethereum-bound gas-consuming work for zero remote compensation. This is analogous to enforcing a non-zero minimum slippage/price floor instead of silently accepting a degenerate `0`.

### Proof of Concept
Using the pallet's own test harness (`bridges/snowbridge/pallets/outbound-queue/src/test.rs`):
```rust
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params);
assert_eq!(fee.local, 698000000);
assert_eq!(fee.remote, 0); // remote compensation silently zeroed despite all non-zero params
```
This same `fee` value is what `SendMessage::validate` (`send_message_impl.rs`) would charge the sender before enqueuing the message — the sender pays `local` only, and the message is committed for Ethereum execution with `remote = 0`, i.e., no relayer gas refund/reward is reserved.

### Citations

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
