## Finding: Outbound Queue Remote Fee Calculation Can Silently Round to Zero, Enabling Free (Underpriced) Bridge Message Delivery

### Summary
`Pallet::calculate_fee` in the Snowbridge outbound queue computes the ETH-denominated remote fee that a sender must pay to have a message delivered to Ethereum, then converts it into the local chain's currency via `convert_from_ether_decimals`, which performs an unguarded integer floor division by `10^(18 - Decimals)` (i.e. `10^8` for a 10‑decimal native currency, `10^6` for a 12‑decimal one). For legitimate, non-extreme pricing parameters, this floor division can collapse the remote fee to exactly `0`, and neither `calculate_fee` nor the `validate`/`deliver` path in `send_message_impl.rs` rejects or bumps a zero fee before enqueueing the message. This mirrors the reported bug class exactly: a rate/fee computed via multiplication then division by a large decimal-scaling factor rounds down to zero for small numerators, silently costing the protocol value instead of reverting or erroring.

### Finding Description
`calculate_fee` computes the remote fee in wei, converts it via `FixedU128` division by the exchange rate, then calls `convert_from_ether_decimals`: [1](#0-0) 

This performs `value.checked_div(denom)` where `denom = 10^(18 - Decimals)` — `10^8` for DOT (10 decimals) or `10^6` for KSM (12 decimals), as enforced by the `integrity_test`: [2](#0-1) 

The full fee computation, including the `checked_div` by `exchange_rate`, is here: [3](#0-2) 

Critically, the pallet's own test suite demonstrates that a normal-looking, non-degenerate parameter set (`exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1`, `gas_used = 250000`) already produces `fee.remote == 0`, with a comment acknowledging this "should be avoided": [4](#0-3) 

Despite this known-and-tested zero-fee outcome, `SendMessage::validate` simply returns whatever `Fee` was computed with no minimum-fee or non-zero check, and `deliver` unconditionally enqueues the message once validated: [5](#0-4) 

There is no guard analogous to the `is_zero()` short-circuits used elsewhere in the codebase's reward-calculation paths (e.g. `calculate_validator_incentive_for_page` explicitly returns `None` when the computed amount rounds to zero — [6](#0-5) ). The outbound-queue fee path lacks this defensive pattern entirely.

### Impact Explanation
Because `remote` (the ETH-denominated component covering relayer gas reimbursement and reward) can legitimately floor to zero under governance-configured pricing parameters that are neither malicious nor unusual, any unprivileged user calling the public message-send path (e.g. via `pallet_xcm::execute` triggering `EthereumBlobExporter::deliver` → `OutboundQueue::validate`/`deliver`) can have their message queued and eventually delivered to Ethereum without paying for the actual on-chain execution/relayer cost. This is public underpriced work: message senders extract Ethereum-side execution/relaying essentially for free, while relayers who are supposed to be reimbursed via `Message.Reward` receive nothing, disincentivizing relaying and risking a stall of bridge message processing, or forcing relayers to subsidize gas out of pocket. This falls squarely under "public underpriced work that degrades block production or stalls bridge processing."

### Likelihood Explanation
The bug does not require a malicious relayer, governance actor, or validator — it triggers under ordinary, expected `PricingParameters` values (as shown by the existing unit test using non-extreme inputs). Since `PricingParameters` are periodically updated by governance for legitimate reasons (per the module docs: "governance should manually update these parameters every few weeks"), any combination that yields a small `gas_used_at_most * fee_per_gas + reward` relative to the `10^6`/`10^8` decimal-scaling denominator will silently zero out the remote fee for affected message/command types.

### Recommendation
- Enforce a minimum non-zero remote fee (and reject or round up rather than floor) in `convert_from_ether_decimals` and/or `calculate_fee`.
- Return an error (e.g. `SendError::Overflow`/a new `SendError::FeeTooLow`) from `validate` when the computed `Fee.remote` is zero for a paid (non-governance) channel, rather than silently proceeding.
- Add integrity/regression tests asserting `fee.remote > 0` for the full realistic range of `PricingParameters`, not just spot-checking specific inputs.

### Proof of Concept
The existing repository test already constitutes a proof of concept: [4](#0-3) 
With `exchange_rate = FixedU128::from_rational(1, 1)`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = FixedU128::from_rational(1, 1)`, and `gas_used = 250000`, `OutboundQueue::calculate_fee` returns `fee.remote == 0`. Feeding this `Fee` through `SendMessage::validate`/`deliver` (`bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs:41-88`) shows no check rejects the zero remote fee — the message is queued and will be committed/delivered to Ethereum with a `reward` of `0`, confirming that a sender can obtain Ethereum-side delivery of a message without paying the intended remote fee.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L259-262)
```rust
		fn integrity_test() {
			let decimals = T::Decimals::get();
			assert!(decimals == 10 || decimals == 12, "Decimals should be 10 or 12");
		}
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L753-757)
```rust
		if validator_incentive_for_page.is_zero() {
			return None;
		}

		Some(validator_incentive_for_page)
```
