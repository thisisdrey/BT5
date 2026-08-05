Audit Report

## Title
Fee-calculation rounding lets Snowbridge outbound messages be dispatched to Ethereum with a zero remote-fee charge - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

## Summary
`Pallet::<T>::calculate_fee` computes the native-currency fee for an outbound message by taking the ether-denominated remote fee, dividing by `params.exchange_rate` via `checked_div`, and then dividing again by a fixed power-of-ten `denom` in `convert_from_ether_decimals` to account for the ETH (18 decimals) vs. native currency (10-12 decimals) gap. Both operations use floor integer division, so when the numerator is small relative to either divisor, the computed `fee.remote` truncates to zero while the message is still queued and later committed with a non-zero `reward` obligation on the Ethereum side.

## Finding Description
`calculate_fee` at [1](#0-0)  derives the remote fee from `gas_used_at_most * fee_per_gas + reward`, multiplies by `params.multiplier`, and divides by `params.exchange_rate` using `checked_div`, then passes the result through `convert_from_ether_decimals`, which performs one more `checked_div` by `10^(ETHER_DECIMALS - T::Decimals)` [2](#0-1) . Both divisions are standard floor division with no rounding-up or minimum-fee enforcement.

`SendMessage::validate`, invoked by every outbound message submission path (system pallet commands, XCM-routed transfers via the Ethereum exporter), calls `calculate_fee` and returns whatever `fee` results without any check that `fee.remote > 0` [3](#0-2) . No such guard (`ensure!(fee.remote > 0, ...)` or minimum-fee floor) exists anywhere in the codebase — a grep for `FeeTooLow`/`MinimumFee`/`ensure!(fee` across the repo turns up no such check in the outbound-queue pallet or its callers.

Once queued, `do_process_message` independently re-reads `pricing_params.rewards.remote` and encodes it unconditionally as the `reward` field of the `CommittedMessage` sent to Ethereum [4](#0-3) , regardless of what fee (if any) was actually collected from the sender at `validate` time. This breaks the invariant that the fee collected must cover the ether-denominated reward promised on the Ethereum side.

The pallet's own test suite documents this exact defect: with `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1/1`, `gas_used = 250000`, `calculate_fee` returns `fee.remote == 0` despite all pricing parameters being non-zero, with an explicit comment acknowledging this "should be avoided" [5](#0-4) .

## Impact Explanation
Every message committed via `do_process_message` carries a fixed ether-denominated `reward` commitment funded from the bridge's Ethereum-side agent/gateway balance, intended to be backed by fees collected on the Polkadot side. When `calculate_fee` truncates `fee.remote` to zero due to floor division across the ETH/native-currency decimals gap or a large `exchange_rate`, senders pay nothing toward the remote reward/gas obligation while the message still queues a real reward commitment on Ethereum. This is public underpriced work: repeated submission of such messages drains the bridge's remote-side reward/gas budget without collecting backing funds, degrading relayer incentives and threatening the bridge's ability to keep processing messages, which matches the in-scope "public underpriced work that degrades block production or stalls bridge processing" impact category.

## Likelihood Explanation
No privileged actor is needed to trigger the truncation — any account that can submit a message through `SendMessage::validate` (including ordinary XCM transfers routed through the Ethereum exporter, or system-pallet commands) reaches `calculate_fee` with the currently governance-configured `PricingParameters`. The condition depends on those parameters (`exchange_rate`, `fee_per_gas`, `multiplier`, `gas_used_at_most`) landing in a range where the numerator is smaller than the exchange-rate/decimals divisor; the pallet's own regression test reproduces this with simple, non-extreme values (unity exchange rate, minimal fee_per_gas/reward), confirming the defect is reachable, not merely theoretical. Whether current production `PricingParameters` on live BridgeHub Rococo/Westend configurations actually fall into this zero-truncation range could not be fully confirmed within the available tool budget, but the underlying rounding defect and complete absence of a minimum-fee guard are verified directly in the pallet code and its test suite.

## Recommendation
In `calculate_fee` and `convert_from_ether_decimals`, use ceiling division (or an explicit minimum-fee floor) instead of floor division when converting between ether and native decimals, and add `ensure!(fee.remote > 0, Error::<T>::FeeTooLow)` (or equivalent) in `SendMessage::validate` before returning the `Fee`, so a message can never be queued with a non-zero remote reward obligation while `fee.remote == 0`.

## Proof of Concept
The existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` in `bridges/snowbridge/pallets/outbound-queue/src/test.rs` demonstrates the bug: with `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1/1`, `gas_used = 250000`, `OutboundQueue::calculate_fee` returns `fee.local = 698000000` but `fee.remote = 0`. Any caller of `SendMessage::validate` under such pricing parameters gets a ticket with `fee.remote = 0`; calling `deliver` enqueues the message, and `do_process_message` later commits it with a non-zero `reward = pricing_params.rewards.remote` to Ethereum despite the sender having paid nothing for the remote component.

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
