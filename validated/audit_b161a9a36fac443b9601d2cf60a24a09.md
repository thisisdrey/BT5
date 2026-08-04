### Title
Remote-fee rounding to zero lets senders under-pay for Ethereum relayer rewards in Snowbridge outbound queue - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` computes the native-currency amount a sender must pay to cover the reward that will later be paid to relayers on Ethereum. The final step of this computation performs integer division by a large decimal-scaling constant (`convert_from_ether_decimals`), and this division can truncate the whole remote-fee component to zero even when all `PricingParameters` are individually non-zero and pass `PricingParameters::validate()`. This is the exact analog of the reported `debt = unsafe_div(debt * frac, 10**18)` bug: a legitimate, non-zero input can be scaled down to zero through unguarded integer division, letting the caller escape payment for value that is still promised/settled elsewhere (the `reward` embedded in the committed message that Ethereum relayers can claim).

### Finding Description
`calculate_fee` computes the remote-fee-in-wei, converts it into a fixed-point local-currency amount, and then calls `convert_from_ether_decimals`, which divides by `10^(ETHER_DECIMALS - T::Decimals)` (e.g. `10^8` for a chain with 10 decimals): [1](#0-0) [2](#0-1) 

`PricingParameters::validate` only checks that `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` are individually non-zero — it does not check that the *resulting local-currency remote fee* clears the `10^8`/`10^6` truncation threshold imposed by `convert_from_ether_decimals`: [3](#0-2) 

The repo's own test proves the exact failure mode: with valid, non-zero `fee_per_gas = 1` and `rewards.remote = 1`, the computed `fee.remote` for a 250,000-gas message is `0`: [4](#0-3) 

This `fee` value (specifically `fee.remote`) is exactly what `SendMessage::validate` returns to the caller (e.g. `EthereumBlobExporter` or `pallet-system::send`) to be charged from the sending account before the message is enqueued: [5](#0-4) 

However, the reward that is embedded in the committed message and later payable to Ethereum relayers via the Gateway contract is taken directly from `pricing_params.rewards.remote` (the raw, un-truncated wei value), independent of what was actually charged: [6](#0-5) 

So the two values diverge: the amount debited from the sender for the remote-cost component (`fee.remote`, in local currency) can be `0`, while the amount promised in the committed message and claimable on Ethereum (`reward`, in wei) remains non-zero. The existing guards — `PricingParameters::validate()` — do not prevent this because they check the *raw* parameters, not the *post-rounding, post-decimal-conversion* fee actually charged.

### Impact Explanation
This breaks the invariant that the fee charged upfront for a message must actually back the reward liability that message creates on the remote side. If governance sets legitimate (individually non-zero) pricing parameters that nonetheless produce a sub-threshold remote-fee-in-wei value (any `fee_per_gas * gas_used_at_most + reward < 10^decimals_gap`), then:
- Every low-gas message sent through the outbound queue is charged `fee.remote = 0` for its remote component, while still committing a message with a non-zero `reward` that relayers can claim on Ethereum out of the bridge's Ethereum-side balance.
- This is "public underpriced work" that degrades the bridge's fee economics and can drain relayer-reward-backing funds without corresponding on-chain payment, i.e. an unbacked-payout condition in the Snowbridge delivery flow.

### Likelihood Explanation
Triggering this does not require a malicious relayer, validator, or governance actor — it is a pure function-of-parameters bug reachable by any ordinary, unprivileged sender of a Snowbridge-bound XCM message once governance parameters happen to fall in the truncation range (which the repository's own unit test demonstrates is trivially reachable with "valid" parameters). The likelihood is bounded by the fact that current production-style parameters (gas price in gwei-scale wei values) are unlikely to fall below the `10^8`/`10^6` threshold, so exploitability depends on the specific `PricingParameters` in force, but the code provides no defensive check to prevent it, unlike the analogous fix applied elsewhere in this same codebase (e.g. `pallet-asset-conversion`'s explicit hardening against "integer rounding produces a zero output").

### Recommendation
Add an explicit check in `calculate_fee` (or in `PricingParameters::validate`) that rejects/errors when the computed `fee.remote` (post `convert_from_ether_decimals`) is zero while the raw remote reward/fee inputs were non-zero — mirroring the recommended `debt != 0` guard in the original report. Concretely, after computing `fee` in `calculate_fee`, assert or return an error if `fee == 0` and `params.rewards.remote != 0` / `gas_used_at_most * params.fee_per_gas + params.rewards.remote != 0`, analogous to the fix already applied in `pallet-asset-conversion`'s `quote_price_*` functions (see `substrate/frame/asset-conversion/src/lib.rs` and the corresponding `pr_11795.prdoc`).

### Proof of Concept
Using the existing test as the reproduction (`bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`):
```
gas_used = 250_000
fee_per_gas = 1
rewards.remote = 1
exchange_rate = 1
multiplier = 1

fee = OutboundQueue::calculate_fee(gas_used, price_params);
// fee.local = 698000000  (charged normally)
// fee.remote = 0         (should back rewards.remote = 1 wei, but truncates to 0)
```
Any sender submitting a message under these (or similarly low) governance-set pricing parameters is charged `0` for the remote-fee component while the committed message still carries `reward = 1` (wei) that Ethereum relayers can claim, demonstrating the payment/liability mismatch. [4](#0-3)

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
