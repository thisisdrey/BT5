### Title
Truncated remote fee can compute to zero without validation, causing free/underpriced Snowbridge message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` in the Snowbridge outbound-queue pallet can return a `Fee` whose `remote` component is `0` even when the configured `PricingParameters` (exchange rate, fee-per-gas, reward) are all non-zero and otherwise valid. This zero value is never validated by the caller (`SendMessage::validate`) before being used as the amount charged to the message sender, mirroring the `pricesWereSafe`-ignored pattern in the external report: a function signals (via its computed output, analogous to a "safe/unsafe" flag) that the value is degenerate, and the caller uses it anyway without checking.

### Finding Description
`calculate_fee` computes the remote fee via a chain of fixed-point math and then truncates to the local decimal precision: [1](#0-0) 

The final step, `convert_from_ether_decimals`, performs an integer `checked_div` by `10^decimals`, which silently rounds small values down to zero: [2](#0-1) 

This is already demonstrated by an existing unit test with realistic, valid, non-zero pricing parameters (`exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`), where `fee.remote` truncates to `0`, and the test comment explicitly flags this as an unresolved issue: [3](#0-2) 

The caller, `SendMessage::validate` in `send_message_impl.rs`, calls `calculate_fee` and returns the resulting `Fee` (`local`, `remote`) directly to the upstream exporter/system pallet to be charged from the sender, with no check that `fee.remote` is non-zero or otherwise "safe" to use: [4](#0-3) 

Meanwhile, the committed message that is actually delivered to Ethereum embeds the relayer's promised `reward` and `max_fee_per_gas` taken straight from the *configured* `pricing_params` (not from the truncated `fee.remote` that was charged to the sender): [5](#0-4) 

This creates a mismatch: the sender is charged `fee.local + fee.remote` where `fee.remote` can degrade to `0` for legitimate parameter combinations, while the Ethereum-side commitment still promises relayers a non-zero `reward`/gas refund. The bridge (or its reserve/treasury covering relayer payouts) ends up subsidizing message delivery that was not actually paid for by the sender — exactly the class of bug the external report describes: a computed value that is not "safe" for its intended use is passed through uninspected by the responsible caller.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" and value-conservation guarantees for bridge rewards: an unprivileged, unpermissioned account (any parachain/user able to send an XCM message that routes through the Snowbridge exporter) can submit messages priced at `fee.local` only, paying nothing for the remote/relayer component while the bridge still promises relayers `pricing_params.rewards.remote` on the Ethereum side. Repeated abuse drains whatever reserve funds relayer rewards, or (if unfunded) causes relayers to have no economic incentive to deliver messages, stalling bridge message processing — both are within the accepted impact categories for this program.

### Likelihood Explanation
No malicious peer, validator, relayer, or governance actor is required — this is triggered purely by parameter combinations that governance would plausibly set (small `exchange_rate`, low `fee_per_gas`/`reward`, or particular `gas_used` values) combined with routine message sending by any user. The existing regression test proves the zero-truncation is reachable with "valid" parameters, meaning the condition is not a contrived edge case but a demonstrated arithmetic property of `calculate_fee`.

### Recommendation
In `calculate_fee` (or immediately in `SendMessage::validate`), reject/ensure `fee.remote` is non-zero (or above some configured minimum) whenever `pricing_params.rewards.remote` and `fee_per_gas` are non-zero, returning a `SendError` instead of silently proceeding with an underpriced fee. Alternatively, use rounding-up division in `convert_from_ether_decimals` for the fee path, and add an explicit invariant check that the charged fee's remote component is sufficient to cover the reward/gas-refund actually committed in the outbound message.

### Proof of Concept
1. Governance sets `PricingParameters { exchange_rate: FixedU128::from_rational(1,1), fee_per_gas: 1, rewards: Rewards { local: 1, remote: 1 }, multiplier: FixedU128::from_rational(1,1) }` (all non-zero, plausible values), as reproduced by the existing test: [3](#0-2) 
2. Any user sends a message with `gas_used_at_most = 250000` through `SendMessage::validate`, which calls `Self::calculate_fee`: [6](#0-5) 
3. The returned `Fee.remote == 0` (per the test) is used unchecked as the amount charged to the sender for remote delivery costs, while `do_process_message` still embeds the full `pricing_params.rewards.remote` reward and `fee_per_gas`-derived `max_fee_per_gas` into the committed Ethereum-bound message: [7](#0-6) 
4. Result: the sender's remote fee payment (0) does not cover the reward/gas-refund promised to Ethereum relayers, an unvalidated discrepancy that is never checked by the caller — directly analogous to ignoring `pricesWereSafe` before use.

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
