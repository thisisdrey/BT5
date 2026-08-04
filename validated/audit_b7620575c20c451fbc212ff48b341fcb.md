## Analysis

I found a structural analog to the Uniswap C-01 bug in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`. The core broken invariant in the original report is: a *rate-based* threshold check (`unitPrice >= thresholdPrice`) is used to gate an action that should be backed by an *absolute* amount, and integer rounding in that rate computation lets the action proceed while the absolute backing amount is insufficient.

The same broken-invariant shape exists in `calculate_fee`: [1](#0-0) 

```
pub(crate) fn calculate_fee(...) -> Fee<T::Balance> {
    let fee = Self::calculate_remote_fee(gas_used_at_most, params.fee_per_gas, params.rewards.remote);
    let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);
    let fee = FixedU128::from_inner(fee)
        .saturating_mul(params.multiplier)
        .checked_div(&params.exchange_rate)
        .expect("exchange rate is not zero; qed")
        .into_inner();
    let fee = Self::convert_from_ether_decimals(fee);
    Fee::from((Self::calculate_local_fee(), fee))
}
```

This exact rounding-to-zero failure mode is demonstrated by the repo's own test: [2](#0-1) 

```
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
    ...
    let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
    assert_eq!(fee.local, 698000000);
    // Though none zero pricing params the remote fee calculated here is invalid
    // which should be avoided
    assert_eq!(fee.remote, 0);
}
```

However, `Self::calculate_fee` output (`fee.remote`) — the amount actually meant to be collected from the sender to cover the promised relayer reward — is **decoupled** from the `reward` value baked into the committed message that is delivered on-chain to Ethereum: [3](#0-2) 

```
let pricing_params = T::PricingParameters::get();
...
let reward = pricing_params.rewards.remote;
let message = CommittedMessage {
    ...
    reward: reward.try_into().defensive_unwrap_or(u128::MAX),
    ...
};
Messages::<T>::append(Box::new(message));
MessageLeaves::<T>::append(message_abi_encoded_hash);
```

`do_process_message` unconditionally commits `reward` (a fixed, non-zero configured value in wei that the Ethereum Gateway contract will pay a relayer) with **no check** that the fee actually collected from the message sender (`fee.remote`, computed via `validate()`/`calculate_fee`) was non-zero or sufficient to back that reward: [4](#0-3) 

```
fn validate(message: &Message) -> Result<(Self::Ticket, Fee<...>), SendError> {
    ...
    let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
    let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
    ...
    Ok((ticket, fee))
}

fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
    ...
    T::MessageQueue::enqueue_message(message, origin);
    ...
}
```

`deliver` (and the downstream `do_process_message` that actually writes the `reward` field into the committed message) never re-validates that the fee amount returned by `validate` was non-zero, nor ties the committed `reward` to what was actually paid. The caller of `SendMessage::validate`/`deliver` (the XCM exporter/router that charges the sender) is expected to withdraw `fee.remote` from the sender and deposit it into the treasury that ultimately backs relayer payouts on Ethereum — but if `fee.remote` rounds to `0` (as the test explicitly proves is possible with "valid" — i.e., non-zero, `PricingParameters::validate()`-passing — pricing parameters), zero DOT is collected from the sender for the remote leg, while the committed message still instructs the Ethereum Gateway contract to pay out `pricing_params.rewards.remote` (non-zero) to whichever relayer delivers it.

This is structurally identical to the Uniswap finding: a rate/ratio-based gate (`unitPrice`/exchange-rate division) is used as a proxy for "enough value has been collected to back a downstream payout," and rounding in that rate computation allows the payout-triggering action (message commitment promising an Ethereum-side reward) to proceed while the actual backing collected is zero.

### Title
Remote fee can round to zero while a non-zero relayer reward is still committed to Ethereum — `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`

### Summary
`OutboundQueue::calculate_fee` can return `fee.remote == 0` (proven by the pallet's own regression test) for otherwise-valid, non-zero `PricingParameters`. Since `fee.remote` is what a sender is charged to fund the relayer reward on Ethereum, but the reward actually promised in the committed message (`reward: pricing_params.rewards.remote`) is taken directly from governance-configured pricing parameters independent of what was collected, a message can be committed and dispatched to Ethereum promising a relayer reward that was never funded by the sender.

### Finding Description
`calculate_fee` performs `FixedU128::from_inner(fee).saturating_mul(multiplier).checked_div(exchange_rate)` then truncates via `convert_from_ether_decimals`, dividing by a power-of-ten denominator. For sufficiently large `exchange_rate` values relative to `fee_per_gas`/`reward`, the intermediate fixed-point value's inner representation can be smaller than the decimal-conversion divisor, causing integer division to floor to `0`, as demonstrated directly by `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`. `PricingParameters::validate()` only checks that each field individually is non-zero — it does not check that the derived fee is non-zero, so this state passes governance validation and can be set via `set_pricing_parameters`. Separately, `do_process_message` always writes `reward: pricing_params.rewards.remote` into the `CommittedMessage`, with no cross-check against the fee that was actually charged when the message was queued via `validate`/`deliver`.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" and "theft or unbacked mint/unlock" categories: an unprivileged user can queue messages that are undercharged for their remote leg (paying `fee.local` only, with `fee.remote == 0`), yet each such message still commits a non-zero `reward` obligation payable to relayers on Ethereum. If enough such messages are sent, the pot of DOT collected to back Ethereum-side relayer payouts becomes insolvent relative to the promised rewards, directly mirroring the Uniswap bug where `enableUniswapV3Launch` could fire without enough ETH actually raised.

### Likelihood Explanation
Likelihood depends on `exchange_rate`/`fee_per_gas`/`reward` parameter values chosen by governance landing in the zero-rounding region; the pallet's own test suite proves such "valid" (non-zero-field) parameter sets exist and produce `fee.remote == 0`. Any unprivileged user sending a message under such parameters triggers the underpriced/unbacked commitment — no privileged actor, relayer collusion, or malicious validator is required, satisfying the "no malicious peer/admin" constraint.

### Recommendation
In `calculate_fee`, reject or clamp fee computations that round to zero for the remote component when `params.rewards.remote` (and thus the promised on-chain reward) is non-zero — e.g., return an error or use `checked_div`/ceiling division so `fee.remote > 0` whenever a reward will be committed. Additionally, `do_process_message`/`send_message_impl::deliver` should assert that the fee actually collected for a message is consistent with (at least covers) the `reward` value it commits, rather than sourcing `reward` solely from live `PricingParameters` independent of what was charged at `validate` time.

### Proof of Concept
The existing unit test already demonstrates the root cause: [2](#0-1) 

To complete the exploit path: set `PricingParameters` (via `system::set_pricing_parameters`) to values in this zero-rounding region (all fields individually non-zero, passing `PricingParameters::validate`), then call `OutboundQueue::validate`/`deliver` for an arbitrary message — `fee.remote` returned will be `0`, so the sender pays nothing for the remote leg, while `do_process_message` still commits `reward: pricing_params.rewards.remote` (non-zero) into the message that is delivered to the Ethereum Gateway contract, which will pay that promised reward to the relayer out of the (unfunded-for-this-message) pot.

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
