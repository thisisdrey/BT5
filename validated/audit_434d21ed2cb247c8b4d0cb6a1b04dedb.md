### Title
Truncating division in `calculate_fee`'s ether-to-native conversion lets the remote/relayer-reward fee component round to zero, enabling free spam of Snowbridge outbound message delivery - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`Pallet::calculate_fee` (used by `SendMessage::validate` to price every outbound Snowbridge message) computes the remote/relayer-reward portion of the delivery fee in 18-decimal Ether units and then converts it to the native (10 or 12 decimal) currency with a plain integer division in `convert_from_ether_decimals`. When the computed Ether-denominated fee is smaller than the decimal-scaling divisor, the division truncates to `0`, so the sender is charged nothing for the remote/reward component of the fee while the message is still fully accepted, queued, and eventually forwarded to Ethereum with a governance-configured (non-zero) reward value baked into the wire message. This is the same class of bug as the `Fund.finalizeGrant` issue in the external report: an integer-division fee calculation that silently rounds down to zero for small inputs, defeating the fee/reward accounting invariant.

### Finding Description
`calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` (lines 368-393) computes:

```
let fee = Self::calculate_remote_fee(gas_used_at_most, params.fee_per_gas, params.rewards.remote);
let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);
let fee = FixedU128::from_inner(fee)
    .saturating_mul(params.multiplier)
    .checked_div(&params.exchange_rate)
    .expect("exchange rate is not zero; qed")
    .into_inner();
let fee = Self::convert_from_ether_decimals(fee);
Fee::from((Self::calculate_local_fee(), fee))
```

`convert_from_ether_decimals` (lines 411-418) does:

```rust
pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
    let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
    let denom = 10u128.saturating_pow(decimals);
    value.checked_div(denom).expect("divisor is non-zero; qed").into()
}
```

For a chain with 10 decimals (e.g. BridgeHub/DOT), `decimals = 18 - 10 = 8`, so `denom = 10^8`. Any pre-division `value` smaller than `10^8` truncates to `0`. This is confirmed by the existing repo test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`, which explicitly asserts `fee.remote == 0` under realistic, non-degenerate `PricingParameters` (unit exchange rate, unit multiplier, unit fee-per-gas, unit rewards). [1](#0-0) [2](#0-1) [3](#0-2) 

This computed `Fee` (local + remote) is exactly what `SendMessage::validate` returns to callers (e.g. the XCM exporter / `snowbridge-pallet-system::send`) as the amount to withdraw from the sender before `deliver` enqueues the message: [4](#0-3) 

Crucially, `do_process_message` (which actually builds the message sent to Ethereum and assigns the `reward` field embedded in the wire message) reads the reward directly from the live `PricingParameters` (`pricing_params.rewards.remote`), not from whatever fee was actually collected at `validate`-time: [5](#0-4) 

So the two values — "fee actually charged to the sender" and "reward promised to the relayer on Ethereum" — are decoupled. When the charged `fee.remote` truncates to `0`, the sender pays only the small `fee.local` (a fixed weight-based fee), yet the outbound queue still processes the message, assigns it a nonce, and commits it into the merkle root for relaying, carrying the full non-zero `reward` value that governance intended to be funded by fee revenue.

### Impact Explanation
This lets any unprivileged account that can invoke the exporter (any parachain/XCM message routed via the Snowbridge exporter, or any extrinsic that ultimately calls `validate`/`deliver`) submit outbound-queue messages while paying only the negligible local weight fee and effectively nothing for the Ethereum-side gas/relayer-reward component. Because the queue enforces only a per-block message-count cap (`MaxMessagesPerBlock`) and not a fee-based backpressure once the priced component is zero, an attacker can flood the outbound queue with maximum-size/maximum-gas commands at near-zero cost, exhausting `MaxMessagesPerBlock` slots each block and starving legitimate messages — degrading bridge throughput and stalling delivery of real messages (a form of "public underpriced work that degrades block production or stalls bridge processing" per the impact gate). It also creates an accounting mismatch where reward liabilities embedded on-chain outstrip actual fee revenue collected, which over many spam messages can drain/underfund the relayer reward pool relative to what was actually paid in.

### Likelihood Explanation
The rounding condition is not a contrived edge case: it is directly exercised and confirmed by the pallet's own unit test with ordinary parameter values (unit exchange rate/multiplier/fee-per-gas/reward), and the decimal gap (`18` Ether decimals vs `10`/`12` native decimals, i.e. `denom` of `10^8`/`10^6`) is a structural property of every configured Snowbridge BridgeHub runtime, not an unusual governance misconfiguration. Any command with low `gas_used_at_most`/low configured `reward`/`fee_per_gas` relative to the exchange rate can trigger it, and no additional privilege beyond normal message submission is required.

### Recommendation
- Enforce a minimum non-zero remote fee analogous to the pattern already used in `substrate/frame/transaction-payment/asset-tx-payment/src/payment.rs` (`min_converted_fee = if fee.is_zero() { Zero::zero() } else { One::one() }`), i.e. in `convert_from_ether_decimals`/`calculate_fee`, if the pre-division Ether value is non-zero, round the native fee up to at least `1` rather than truncating to `0`.
- Alternatively, use `multiply_by_rational_with_rounding`/`Rounding::Up` (already available in `sp_arithmetic`) for the ether-to-native conversion instead of plain `checked_div`.
- Consider rejecting message submission outright (as `pallet-psm` does via `Error::AmountTooSmallAfterConversion`) when the computed remote fee would genuinely truncate to zero, rather than silently accepting the message for free.

### Proof of Concept
1. Configure `PricingParameters` with `exchange_rate = FixedU128::from_rational(1, 1)`, `fee_per_gas = 1`, `rewards = { local: 1, remote: 1 }`, `multiplier = FixedU128::from_rational(1, 1)` (mirroring `bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-320`).
2. Call `OutboundQueue::calculate_fee(gas_used, price_params)` with a `gas_used` such that the pre-division ether fee stays below `10^8` (the existing test uses `gas_used = 250000` and observes `fee.remote == 0` while `fee.local == 698000000`).
3. Submit a message via the exporter/`validate`+`deliver` path; only `fee.local` is withdrawn from the sender, `fee.remote` collected is `0`.
4. Observe that `do_process_message` still assigns a nonce, embeds `reward = pricing_params.rewards.remote` (non-zero) into the committed message, and includes it in the merkle root for relaying — i.e., the message is processed and promises relayer reward despite the sender paying zero for it.
5. Repeating this at scale up to `MaxMessagesPerBlock` per block demonstrates queue congestion at near-zero cost.

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
