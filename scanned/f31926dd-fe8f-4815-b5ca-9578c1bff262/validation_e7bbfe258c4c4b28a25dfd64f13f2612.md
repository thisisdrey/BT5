### Title
Integer-division truncation in `OutboundQueue::calculate_fee` lets the remote (Ethereum-side) delivery fee round down to zero, causing underpriced bridge work - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Snowbridge outbound queue computes the DOT fee a user must pay to cover Ethereum-side relayer gas/reward via `Pallet::calculate_fee`. The computation converts an 18-decimal ether-denominated value into the chain's native decimals using plain integer division (`checked_div`), with no minimum-fee floor. When `gas_used_at_most * fee_per_gas + reward` (in wei) is small relative to the decimal-conversion divisor, the result truncates to zero, so the sender is charged `fee.remote = 0` even though all `PricingParameters` are non-zero and pass `validate()`.

### Finding Description
`calculate_fee` in [1](#0-0)  computes:

```
fee = FixedU128::from_inner(remote_fee_wei)
    .saturating_mul(multiplier)
    .checked_div(exchange_rate)
    .into_inner();
fee = convert_from_ether_decimals(fee);
```

`convert_from_ether_decimals` does a plain integer divide with no rounding-up or floor protection: [2](#0-1) . This is analogous to the external report's root cause: a conversion routine silently assumes the two units it's translating between (wei-scale remote value vs. native-decimal local value) always land on a "safe" ratio, when in fact the exchange rate/decimals combination can produce a non-representable (rounds-to-zero) result — just as DIA's `/USD` peg assumption silently breaks when the peg diverges.

The repository's own test suite documents this exact failure with a comment acknowledging it should not happen: [3](#0-2) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `gas_used = 250000`, and `Decimals = 12`: `remote_fee_wei = 250001`; after fixed-point math the pre-decimal-adjustment value is `250001`; dividing by `10^(18-12) = 10^6` truncates to `0`. `fee.remote` ends up `0` despite `PricingParameters::validate()` accepting the parameters (it only rejects exact zero, not "effectively zero after truncation").

This truncated `fee` is exactly what is returned from `SendMessage::validate` in [4](#0-3)  and used by callers (the XCM exporter / benefit-charging logic) to withdraw payment from the sender via `Fee::total()` = `local + remote` [5](#0-4) . Meanwhile the actual committed message that gets delivered to Ethereum still carries the full `max_fee_per_gas` and `reward` taken directly (untruncated) from `PricingParameters` in `do_process_message` [6](#0-5) . So the on-chain commitment promises relayers a real reward/gas refund on Ethereum, but the local DOT fee actually collected from the sender to fund that promise can be zero — an unpriced obligation is queued for delivery.

### Impact Explanation
This is public, unprivileged underpriced work with direct chain/bridge impact: any ordinary user calling into the bridge (e.g. via `pallet_xcm::execute`/`send`) that triggers `OutboundQueue::validate` pays `fee.remote = 0` under low `gas_used_at_most * fee_per_gas + reward` / decimals-divisor ratios, while the queued message still commits the bridge to reward relayers on Ethereum. Repeated exploitation drains the pool funding relayer rewards without collecting matching fees, degrading relayer incentives and stalling bridge message processing — matching the explicitly accepted impact category "public underpriced work that degrades block production or stalls bridge processing."

### Likelihood Explanation
Likelihood depends on governance-chosen `PricingParameters` (`exchange_rate`, `fee_per_gas`, `multiplier`) and the chain's configured `Decimals` relative to `ETHER_DECIMALS` (18). Because these parameters are set/adjusted periodically by governance (as documented in the module comment: "governance should manually update these parameters every few weeks") [7](#0-6) , any period where the resulting scaled value falls below the decimal-conversion divisor reproduces the truncation-to-zero shown by the repo's own test — this is not a contrived edge case but one the maintainers already wrote a regression test for, and `validate()` does not guard against it since it only checks for exact-zero inputs, not for the truncated output.

### Recommendation
In `convert_from_ether_decimals` (and analogous decimal-scaling steps in `calculate_fee`), round up instead of truncating, or enforce a configurable minimum non-zero fee floor so that any strictly-positive computed fee cannot collapse to zero after decimal conversion. Additionally, extend `PricingParameters::validate` (or add a runtime check in `calculate_fee`) to reject/flag parameter combinations that would yield `fee.remote == 0` for realistic `gas_used_at_most` values.

### Proof of Concept
Existing repository test demonstrates the exact zero-fee outcome with non-zero, "valid" pricing parameters: [3](#0-2) 
Steps: set `PricingParameters{ exchange_rate: 1/1, fee_per_gas: 1, rewards: {local:1, remote:1}, multiplier: 1/1 }`, call `OutboundQueue::calculate_fee(250000, price_params)`. Result: `fee.local = 698000000` (nonzero, correctly charged), but `fee.remote = 0`, meaning the sender pays nothing toward the Ethereum-side reward/gas that the queued `CommittedMessage` (built from the same `PricingParameters.rewards.remote` / `fee_per_gas`, unaffected by truncation) still promises to relayers on delivery.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-55)
```rust
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L280-287)
```rust
impl<Balance> Fee<Balance>
where
	Balance: BaseArithmetic + Unsigned + Copy,
{
	pub fn total(&self) -> Balance {
		self.local.saturating_add(self.remote)
	}
}
```
