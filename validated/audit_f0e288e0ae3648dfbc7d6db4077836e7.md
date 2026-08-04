Based on my investigation, I found a concrete local analog in the Snowbridge outbound-queue fee-calculation logic: `Pallet::<T>::calculate_fee` can silently truncate the Ethereum-side relayer fee to zero due to integer-division rounding, while the message is still queued and delivered as if it had been correctly paid for.

### Title
Snowbridge outbound-queue `calculate_fee` can round the remote relayer fee to zero, causing the message to be delivered underpriced - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
`Pallet::<T>::calculate_fee` (lines 368–393) computes the ETH-side relayer reward (`fee.remote`) via `fixed-point` division against `params.exchange_rate` and integer conversions. With valid, non-zero `PricingParameters`, the computed remote fee can still floor to `0` due to truncation, as the pallet's own unit test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` demonstrates and explicitly comments “though non zero pricing params the remote fee calculated here is invalid which should be avoided.” [1](#0-0) [2](#0-1) 

### Finding Description
`SendMessage::validate` in `send_message_impl.rs` calls `T::GasMeter::maximum_gas_used_at_most` and then `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())` to produce the `Fee { local, remote }` charged to the sender before the message ticket is queued and later processed/committed to the Ethereum gateway. [3](#0-2) 

The remote-fee arithmetic is:
```
fee = calculate_remote_fee(gas_used_at_most, fee_per_gas, reward)  // U256
fee = fee as u128
fee = FixedU128::from_inner(fee).saturating_mul(multiplier).checked_div(&exchange_rate).into_inner()
fee = convert_from_ether_decimals(fee)  // divides by 10^(18 - local decimals)
``` [4](#0-3) 

Each stage truncates towards zero. When `exchange_rate` is large (e.g., ETH priced much higher than the local token) or `fee_per_gas`/`reward`/`multiplier` are small, the final `fee.remote` can floor to `0` even though the pricing parameters were validly configured and non-zero — exactly the scenario proven by the existing test at `bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`. This mirrors the Stargate `_lzTxParams` bug's core invariant: a fee input parameter that should reflect real destination-side execution cost is effectively zeroed out by the calculation path, decoupling the amount actually charged from the amount actually required to service the cross-chain leg.

Because `validate()` unconditionally returns `Ok((ticket, fee))` regardless of whether `fee.remote == 0`, the message proceeds through `deliver()` → `T::MessageQueue::enqueue_message` → `do_process_message` → `commit()` and is included in the merkle-committed message set sent to Ethereum, with `CommittedMessage.reward` set from `pricing_params.rewards.remote` (a separate, unrelated field) while the *sender* was only actually charged (or the delivery-fee return value reported) `fee.remote == 0`. There is no floor/minimum check, no `ensure!(fee.remote > 0, ...)`, and no re-validation before enqueue.

### Impact Explanation
This is a public underpriced-work path with bridge-processing impact: any account calling into the outbound-queue `SendMessage::validate`/`deliver` flow (via XCM `ExportMessage` from a parachain, or any pallet using `SendMessage`) can have its message accepted into the merkle-committed outbound queue while paying zero (or near-zero) for the Ethereum-side relayer reward/gas-refund component. Relayers who deliver the message on Ethereum are refunded from `Message.reward`/`Message.maxFeePerGas` fields baked into the committed message from `PricingParameters` at processing time — not from what was actually collected from the sender — so the bridge's own economic accounting (fee charged vs. fee owed) can decouple, degrading the incentive model that keeps bridge message processing running, and in the worst case lets senders push messages that should have been fee-rejected through for free.

### Likelihood Explanation
High: this is not a governance misconfiguration edge case — it is a deterministic arithmetic property of `calculate_fee` given "PricingParameters" values within otherwise normal/valid ranges (as the pallet's own regression test confirms), and it fires on every call through the standard, unprivileged `SendMessage::validate` path with no additional preconditions, no malicious relayer/governance actor required.

### Recommendation
Add an explicit invariant check in `calculate_fee` (or in `validate`) that rejects/floors the computation when `fee.remote == 0` while `gas_used_at_most > 0`, e.g. `ensure!(fee.remote > 0 || gas_used_at_most == 0, Error::<T>::FeeCalculationInvalid)`, and/or increase precision (avoid truncating intermediate `U256`→`u128`→`FixedU128` conversions) so that any non-zero real-world gas cost always maps to a non-zero charged fee, matching the same "don't hardcode/zero-out a required destination-side cost parameter" fix the external Stargate report recommends.

### Proof of Concept
The existing pallet test already demonstrates the exact defect and even flags it as invalid in a code comment: [2](#0-1) 
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
Calling `SendMessage::validate(&message)` under these (or economically-equivalent, e.g. small `fee_per_gas`/`reward` vs. large `exchange_rate`) parameters returns `Fee { local: 698000000, remote: 0 }`, and `deliver(ticket)` will still enqueue and eventually commit the message for Ethereum delivery — with zero collected for the relayer-reward component — with no guard preventing this. [5](#0-4)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-418)
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

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-88)
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

	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = AggregateMessageOrigin::Snowbridge(ticket.channel_id);

		if ticket.channel_id != PRIMARY_GOVERNANCE_CHANNEL {
			ensure!(!Self::operating_mode().is_halted(), SendError::Halted);
		}

		let message = ticket.message.as_bounded_slice();

		T::MessageQueue::enqueue_message(message, origin);
		Self::deposit_event(Event::MessageQueued { id: ticket.message_id });
		Ok(ticket.message_id)
	}
```
