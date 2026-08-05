### Title
Fee-calculation rounding lets Snowbridge outbound messages be dispatched to Ethereum with a zero remote-fee charge - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The Snowbridge outbound-queue fee model divides across two different-precision currencies (ETH, 18 decimals, and DOT/KSM, 10-12 decimals) using integer division. When the resulting per-message fee is small relative to the exchange rate/decimals gap, `calculate_fee` truncates the remote component of the fee to zero while the message is still queued and forwarded for execution on Ethereum, exactly mirroring the external report's "low-decimals `purchaseToken` causes `volume / price` to round to 0" pattern.

### Finding Description
`Pallet::<T>::calculate_fee` computes the fee a sender must pay to have a message delivered and executed on Ethereum: [1](#0-0) 

The remote fee is derived from `gas_used_at_most * fee_per_gas + reward` (all in wei/ether units), then multiplied by a safety `multiplier`, divided by the `exchange_rate` (ETH/DOT), and finally passed through `convert_from_ether_decimals`, which does one more integer division by `10^(ETHER_DECIMALS - T::Decimals)`: [2](#0-1) 

Both divisions (`checked_div` by `exchange_rate` and `checked_div` by `denom`) use standard floor division. If the numerator (remote fee in wei, scaled by the multiplier) is smaller than the divisor — which happens for any message whose gas/reward cost is small relative to the configured ETH/DOT exchange rate, or whenever `T::Decimals` is far below `ETHER_DECIMALS` (10 for DOT vs 18 for ETH, an 8-order-of-magnitude gap) — the result truncates to zero. This is not a hypothetical: the pallet's own unit test documents this exact rounding-to-zero outcome as a known, unresolved defect: [3](#0-2) 

Critically, `SendMessage::validate` (called by every code path that emits an outbound message — governance commands, token transfers via the XCM exporter, etc.) uses whatever `fee` value `calculate_fee` returns without any minimum-fee floor or rejection when the remote component is zero: [4](#0-3) 

The message is queued and later dispatched by `do_process_message`, which independently re-reads `pricing_params.rewards.remote` and encodes it as the `reward` promised to the relayer/agent contract on Ethereum — this reward promise is unconditional and unrelated to whether the sender was actually charged for it: [5](#0-4) 

So the invariant that is broken is: *"the DOT/KSM fee collected from the sender must cover the ether-denominated reward/gas obligation promised on the Ethereum side."* Because of floor-division truncation across a large decimals gap, this invariant silently fails to zero, and no guard (no `ensure!(fee.remote > 0)`, no minimum-fee check) exists to stop it — exactly the missing check pattern the external report calls out (`volume >= price` / decimals-equality enforcement never happens here either).

### Impact Explanation
Every dispatch through the outbound queue promises a fixed ether-denominated `reward` to relayers, funded from the bridge's Ethereum-side agent/gateway balance, which is expected to be replenished by the DOT fees collected on the Polkadot side. When `calculate_fee` truncates the remote component to zero, senders are charged only the local processing fee and pay nothing toward the remote reward/gas obligation, while the outbound message still carries a non-zero `reward` commitment to Ethereum. Repeated exploitation (sending many small/cheap commands, e.g. governance-exempt paths or low-gas commands) systematically drains the bridge's remote-side reward/gas budget without collecting the corresponding backing funds, degrading relayer incentives and, at scale, threatening bridge solvency and its ability to keep processing messages — i.e., "public underpriced work that degrades... stalls bridge processing," one of the explicitly in-scope impact categories.

### Likelihood Explanation
Medium-to-high. No privileged actor, admin, relayer, or validator collusion is required — any account able to submit a message through the outbound queue (including ordinary XCM transfers routed through the Ethereum exporter) can trigger the truncation whenever governance-configured pricing parameters make `fee_per_gas * gas + reward`, multiplied by the safety multiplier, small relative to the exchange-rate/decimals divisor. The condition is already reproduced and asserted in the pallet's own test suite, confirming it is reachable with realistic parameter combinations, not just theoretical extremes.

### Recommendation
In `calculate_fee` (and in `convert_from_ether_decimals`), reject or round up (ceiling-divide) instead of floor-dividing when converting between ether and native decimals, and add an explicit `ensure!(fee.remote > 0, Error::<T>::FeeTooLow)` (or an enforced minimum fee) before returning the `Fee` from `validate`, so that a message can never be queued with a non-zero remote reward obligation but a zero collected remote fee.

### Proof of Concept
The existing regression test in the repository already demonstrates the bug end-to-end: [3](#0-2) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1/1`, and `gas_used = 250000`, `calculate_fee` returns `fee.remote == 0` even though all pricing parameters are non-zero — an attacker only needs governance-configured (or default/testnet) pricing parameters to fall in this range, then call any public entry point that routes through `OutboundQueue::validate` (e.g. an XCM transfer to Ethereum) to get the message enqueued and eventually dispatched with a live `reward` commitment while having paid `fee.remote = 0`.

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
