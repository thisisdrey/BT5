### Title
Reward-precision truncation lets Snowbridge outbound messages be enqueued with a computed relayer reward that rounds to zero, enabling underpriced spam of the bridge delivery queue - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` computes the fee a user must pay to send a command to Ethereum. The "remote" portion of the fee (which becomes the relayer `reward` embedded in the committed message) is downscaled from Ether's 18-decimal precision to the local chain's decimal precision via integer division in `convert_from_ether_decimals`. For legitimate, non-attacker-controlled `PricingParameters` and small `gas_used_at_most` values, this division truncates the remote fee to `0`, as demonstrated by the repo's own test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`. Because nothing in `do_process_message` or `calculate_fee` enforces a non-zero minimum reward, a message can be queued and committed to Ethereum with `reward: 0` while consuming outbound queue capacity and relayer gas — an on-chain analog of the LBTC `burn` bug where an unenforced/degenerate fee (`0`) allowed unbounded low-value operations to flood downstream infrastructure.

### Finding Description
`calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` computes: [1](#0-0) 

The remote component is computed in wei-equivalent units, then scaled down with `convert_from_ether_decimals`, which performs plain integer division by `10^(18 - local_decimals)`: [2](#0-1) 

For small `gas_used_at_most` and small `reward`/`fee_per_gas` pricing values (both are governance-set, not attacker controlled, but can legitimately be modest), the numerator (`gas*fee_per_gas + reward`) can be smaller than the divisor `10^8`–`10^16` used for DOT/KSM decimal conversion, causing the result to floor to `0`. The repository's own unit test confirms this exact degenerate case occurs with plausible, non-zero pricing parameters: [3](#0-2) 

This truncated `reward` (0) is then embedded directly into the `CommittedMessage` sent to Ethereum in `do_process_message`, with no minimum-reward check: [4](#0-3) 

The `validate`/`deliver` path in `send_message_impl.rs`, used by any public XCM/asset-transfer flow that routes through the exporter, only checks payload size and channel validity — it never verifies that the returned `Fee` (and therefore the embedded relayer reward) is above some minimum: [5](#0-4) 

This mirrors the LBTC bug's root cause: a fee variable that can silently be (or become) zero with no enforced floor, letting an unprivileged actor push arbitrarily many low/no-cost units of work into a queue that a downstream off-chain/on-chain component must service (here, Ethereum relayers and the bounded `MessageLeaves`/`MaxMessagesPerBlock` outbound queue).

### Impact Explanation
Any user can construct outbound Ethereum commands (e.g., small ERC20 transfers or register-token calls) that fall into gas ranges producing a truncated zero (or near-zero) `reward`. Because the local fee only covers weight/processing cost on the sending chain and not any minimum incentive for the remote relayer, an attacker can flood the outbound queue with messages that:
1. Consume the bounded `MessageLeaves`/`MaxMessagesPerBlock` capacity each block (`ensure!(... < T::MaxMessagesPerBlock::get(), Yield)`), delaying or crowding out legitimate/governance-adjacent messages.
2. Get committed to Ethereum with `reward: 0`, giving relayers no incentive to process them, causing them to pile up unprocessed on the Ethereum side — a stall of bridge processing that matches the "public underpriced work that degrades block production or stalls bridge processing" impact category.

This does not require a malicious relayer, validator, or governance actor — only a normal, unprivileged user triggering ordinary bridge sends with parameters chosen to land in the truncation range.

### Likelihood Explanation
Likelihood is moderate-to-high in principle: the exact truncation trigger depends on governance-configured `PricingParameters` (fee_per_gas, reward, exchange_rate, multiplier) and target chain decimals, so whether it is exploitable in a specific deployed runtime depends on those live values. However, the repository's own test demonstrates the flooring-to-zero behavior occurs with realistic parameter magnitudes, and there is no code path anywhere in `calculate_fee`, `do_process_message`, or `validate` that clamps the result to a nonzero floor — so the guard that would prevent this (a minimum reward/fee check, analogous to LBTC's intended `burnCommission` floor) simply does not exist.

### Recommendation
Enforce a minimum non-zero `remote` fee/reward in `calculate_fee` (e.g., round up instead of floor, or reject/clamp results below a configured minimum) before the fee is returned from `validate` and before `reward` is embedded into the `CommittedMessage` in `do_process_message`. Add a defensive check in `do_process_message` that rejects committing a message whose computed `reward` is zero, mirroring the LBTC fix's approach of enforcing a minimum operative fee.

### Proof of Concept
Using the existing test harness values from `bridges/snowbridge/pallets/outbound-queue/src/test.rs`:
```rust
let gas_used: u64 = 250000;
let price_params: PricingParameters<Balance> = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params);
assert_eq!(fee.remote, 0); // relayer reward truncates to zero
```
Repeating a `send_message`/XCM transfer that resolves to this fee profile `N` times enqueues `N` messages into `MessageLeaves` (bounded only by `MaxMessagesPerBlock` per block) and commits `N` `CommittedMessage`s to Ethereum with `reward: 0`, at negligible cost to the attacker beyond ordinary local weight fees. [6](#0-5)

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
