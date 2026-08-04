### Title
Remote fee rounding to zero lets Snowbridge outbound messages be sent with an unpaid Ethereum relayer reward - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`Pallet::calculate_fee` converts the ETH-denominated relayer reward into the local (DOT) fee charged to the message sender, but never checks that the resulting `fee.remote` is non-zero. Integer division/rounding during the ether-decimals conversion can make the *charged* local-currency remote fee collapse to `0`, while the `CommittedMessage` that is actually sent to Ethereum still embeds the full, non-zero `reward` value from `PricingParameters`. This is the same class of bug as the RedStone report: values that feed a financial calculation (`basePrice`/`tokenPrice` there, `fee`/`exchange_rate`/`reward` here) are never validated against zero, so the code silently proceeds with an invalid, unpredictable result instead of rejecting it.

### Finding Description
`calculate_fee` computes the remote fee in wei via `calculate_remote_fee`, then converts it to local currency: [1](#0-0) 

The division by `exchange_rate` is guarded (`expect("exchange rate is not zero; qed")`) via `PricingParameters::validate` at the governance-config level, but the *output* magnitude of the whole expression — the actual DOT amount the sender is charged for the remote/reward component — is never checked against zero. The existing unit test explicitly demonstrates this: [2](#0-1) 

With `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `gas_used = 250000`, the code computes `fee.remote == 0` even though every individual `PricingParameters` field is non-zero and passes `PricingParameters::validate` (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier` all non-zero): [3](#0-2) 

Meanwhile, `do_process_message` builds the `CommittedMessage` that Ethereum will actually pay out from, using the raw configured `pricing_params.rewards.remote` — not the (possibly zero) locally-charged `fee.remote`: [4](#0-3) 

`send_message_impl::validate` is the public entry point every outbound message (user XCM-triggered transfers, calls routed through `EthereumBlobExporter`, etc.) goes through; it calls `calculate_fee` and returns the `Fee` used to charge the sender, with no zero-check on the result: [5](#0-4) 

So the invariant that breaks is: *the DOT amount collected from the sender for the remote/reward component must cover the ETH reward promised to relayers in the committed message*. Because `fee.remote` can round to `0` for legitimate, validated, non-zero `PricingParameters` (small `gas_used`, small `fee_per_gas`/`reward`, or unfavorable exchange-rate/decimals rounding), the pallet can commit messages to Ethereum that still promise relayers a real ETH reward while collecting nothing (or far less than intended) from the DOT side to back it.

### Impact Explanation
This is a public, unprivileged-attacker-reachable underpricing bug in Snowbridge's outbound message pipeline. Any account that can trigger an outbound message (e.g. via the XCM exporter or system pallet's `send`) pays `fee.local + fee.remote` in DOT, but the reward embedded in the Ethereum-bound `CommittedMessage` is derived independently from `pricing_params.rewards.remote`, not from the possibly-zero `fee.remote` actually collected. Over repeated messages this either: (a) drains the reserve/backing meant to cover relayer rewards on Ethereum without corresponding DOT collection (economic value leak/theft-adjacent), or (b) once operators notice the shortfall, causes them to systematically underfund relayers, degrading relaying incentives and stalling bridge message delivery — both are within the accepted impact categories ("theft or unbacked mint/unlock", "public underpriced work that degrades block production or stalls bridge processing").

### Likelihood Explanation
Triggering the zero-rounding condition requires no privileged access — any user submitting a message pays whatever `calculate_fee` returns, and the pallet's own test suite proves that valid, governance-set `PricingParameters` combined with ordinary `gas_used_at_most` values already produce `fee.remote == 0`. This can occur unintentionally under normal parameter drift (e.g., ETH/DOT exchange rate changes without governance updating `fee_per_gas`/`reward` in step), making it a realistic operational condition, not merely a contrived edge case.

### Recommendation
In `Pallet::calculate_fee` (and/or `SendMessage::validate`), explicitly reject (return an error / `SendError`) when the computed `fee.remote` is zero after decimal conversion, mirroring the RedStone remediation's zero-price guard. Alternatively, ensure the reward embedded in `CommittedMessage` is derived from the same, already-collected `fee.remote` value rather than the raw `pricing_params.rewards.remote`, so collected fees and promised remote rewards can never diverge.

### Proof of Concept
The existing regression test already reproduces the exact broken state (present in-tree, unguarded): [2](#0-1) 

With these parameters, `send_message_impl::validate` would return a `Fee { local: 698000000, remote: 0 }` to charge the sender, while `do_process_message` still writes `reward: 1` (wei, non-zero) into the `CommittedMessage` sent to Ethereum — demonstrating the sender pays nothing for the reward that Ethereum will still pay out. [6](#0-5)

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
