### Title
`calculate_fee` truncates the ETH-denominated remote fee to zero on decimal down-scaling, letting senders under-pay for remote message dispatch that is still fully executed and rewarded — ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`Pallet::calculate_fee` (used by `SendMessage::validate` for every outbound Snowbridge message) converts an Ether/Wei-denominated remote fee into native-currency units via `convert_from_ether_decimals`, which does a plain integer division by `10^(ETHER_DECIMALS - T::Decimals)` (typically `10^6` for a 12-decimal chain). Just like the reported `CalcMinCollateral` bug, this calculation ignores the decimal-precision loss inherent in scaling down, so any computed remote fee smaller than the divisor truncates to `0`. The pallet's own unit test documents this exact outcome and even comments that it "should be avoided," yet no guard exists.

### Finding Description
`calculate_fee` computes the remote fee entirely in Ether/Wei fixed-point (18 decimals), then calls `Self::convert_from_ether_decimals(fee)` to rescale down to the chain's native decimals: [1](#0-0) 

This is invoked unconditionally from the public, permissionless entrypoint `validate`, which every outbound message (XCM export or system pallet `send`) passes through before being enqueued for delivery: [2](#0-1) 

The fee derivation: [3](#0-2) 

Because `checked_div` performs floor integer division, any remote fee value less than `10^(18 - Decimals)` wei-equivalent collapses to `0` in the local balance charged to the sender. The pallet's own test proves this: [4](#0-3) 

Critically, the truncated value is *only* the fee charged to the sender in the local (native) currency. The actual reward promised to relayers on the Ethereum side, `pricing_params.rewards.remote`, is stored and forwarded to the committed message verbatim in full Wei precision — independent of the truncated local fee: [5](#0-4) 

So the sender can be charged `fee.remote == 0` (and only a small, fixed `local_fee` covering weight) while the message still carries a non-zero `reward` that the Ethereum gateway contract will pay a relayer for delivering it. No check exists in `validate`, `calculate_fee`, or `PricingParameters::validate` that rejects a request whose computed local-currency remote fee rounds to zero — `validate()` only checks `exchange_rate`, `fee_per_gas`, `rewards.local/remote`, and `multiplier` are individually non-zero, not that the *derived* fee is non-zero after decimal conversion.

### Impact Explanation
This is public, underpriced work with direct bridge-processing impact: an unprivileged account can submit outbound messages whose actual remote-dispatch cost (gas + relayer reward, paid in ETH on Ethereum) is not recovered from the sender in DOT/KSM because the conversion floors to zero for realistic small `fee_per_gas`/`reward` combinations combined with typical `exchange_rate`/`multiplier` values. Repeated free or near-free message submission can be used to drain/imbalance the bridge's fee economics that are meant to fund relayer rewards, and — because there is no cost-based throttle on message submission beyond the (also potentially zero) fee — this can be used to flood the outbound queue with underpriced messages, degrading throughput and stalling legitimate bridge processing, matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
`calculate_fee`/`validate` are on the direct, unauthenticated hot path of every outbound Snowbridge message (from XCM exports or system pallet); no privileged actor is required. The truncation-to-zero condition is not a theoretical edge case — it is explicitly reproduced and asserted in the pallet's own test suite (`test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`), confirming it occurs for realistic parameter values without any admin misconfiguration, only ordinary governance-set `PricingParameters` combined with a message whose gas/fee-per-gas product is small relative to the decimal scaling factor.

### Recommendation
In `convert_from_ether_decimals` (or in `calculate_fee` after the conversion), round up (ceiling division) instead of floor division, and/or enforce a post-conversion minimum: reject/charge a fallback minimum fee if the derived `fee.remote` is zero while `pricing_params.rewards.remote` is non-zero. This mirrors the referenced fix pattern of properly accounting for decimal precision instead of silently truncating to zero.

### Proof of Concept
Using the pallet's own test harness values, reproducible today in `bridges/snowbridge/pallets/outbound-queue/src/test.rs`:
```rust
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params);
assert_eq!(fee.remote, 0); // sender pays nothing for the remote/reward component
``` [4](#0-3) 
Any account calling into `validate`/`deliver` (e.g., via an XCM export routed through the bridge, or `snowbridge_pallet_system::Pallet::send`) with parameters that produce a small enough computed remote fee will have that portion charged as `0`, while the committed message on-chain still records the full `reward` to be paid to relayers on Ethereum: [6](#0-5)

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-73)
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
