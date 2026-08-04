## Analog Found

The OCT report's core defect is: a value that funds/prices cross-chain economic security is allowed to become `0` because only the *raw input* is validated, not the *value actually charged/used downstream*. The same pattern exists in Snowbridge's outbound-queue fee pipeline.

### Title
Outbound-queue fee calculation can silently round `fee.remote` to zero despite validated non-zero pricing parameters, allowing free/underpriced Ethereum-bound message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`PricingParameters::validate()` rejects zero `exchange_rate`, `fee_per_gas`, `rewards`, and `multiplier` at the point they are set by governance via `set_pricing_parameters`. However, the pallet never checks that the *derived* delivery fee actually charged to a message sender (`fee.remote`) is non-zero. Integer-division truncation inside `Pallet::calculate_fee`/`convert_from_ether_decimals` can silently produce `fee.remote == 0` for legitimate, non-zero pricing parameters and normal gas usage, and this zero fee is accepted as valid payment in `send_message_impl.rs`.

### Finding Description
`PricingParameters::validate` only guards the stored parameters themselves: [1](#0-0) 

The actual per-message fee is computed in `Pallet::calculate_fee`, which converts a wei amount down to native-currency decimals via integer division: [2](#0-1) 

Because `convert_from_ether_decimals` divides by `10^(ETHER_DECIMALS - Decimals)` (e.g. `10^8` for a 10-decimal native token), any wei value below that divisor truncates to `0`, regardless of the validated non-zero `exchange_rate`, `fee_per_gas`, `rewards.remote`, and `multiplier`. The pallet's own test acknowledges this exact defect: [3](#0-2) 

This `fee` (including the zero `fee.remote`) is then returned directly from `validate()` as the amount that will be charged to whoever calls `send`/`deliver` (e.g. via `EthereumBlobExporter` or `snowbridge_pallet_system::Pallet::send`), with no floor check that `fee.remote > 0`: [4](#0-3) 

`fee.remote` is the component meant to reimburse relayers for Ethereum-side gas and pay the cross-chain reward (per the module docs: `RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward`, adjusted by exchange rate/multiplier). When it truncates to zero, the sender pays nothing for this leg while the message is still queued, committed, and eventually relayed to Ethereum for real gas cost.

### Impact Explanation
This is a public, underpriced-work condition on the bridge's outbound path: any unprivileged account or sibling parachain that can submit messages through the `EthereumBlobExporter`/outbound queue can construct or naturally trigger message parameters (gas usage, current governance-set pricing) that round the remote fee to zero. Because the remote fee is the mechanism funding relayer gas reimbursement and rewards, it being silently zero degrades relayer incentives and the bridge's anti-spam/economic-security assumption for the remote leg — it does not require a malicious governance actor, relayer, or validator, matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
The pallet's own regression test proves the condition is reachable with valid, non-zero, governance-set parameters and ordinary gas usage — it is not a contrived edge case requiring adversarial parameter choices. Since pricing parameters (`exchange_rate`, `fee_per_gas`) are periodically tuned by governance and gas usage varies per command, the ranges where `fee.remote` rounds to zero can occur during normal operation, not only via intentional manipulation.

### Recommendation
Add an explicit floor check in `Pallet::calculate_fee` (or in `send_message_impl::validate`) that returns an error (e.g. reuse/extend `Error::<T>::InvalidChannel`-style validation) if the computed `fee.remote` (or total fee) is zero, rather than silently accepting a zero fee as valid payment.

### Proof of Concept
Using the pallet's own test harness (`bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`):
```rust
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params);
assert_eq!(fee.remote, 0); // non-zero validated params -> zero remote fee charged
```
This demonstrates that `validate()` (called from `send_message_impl::validate`) would return `Ok((ticket, fee))` with `fee.remote == 0`, allowing the message to be enqueued and later delivered to Ethereum without the sender paying the remote-leg fee.

### Citations

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
