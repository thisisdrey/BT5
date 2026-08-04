I have enough evidence to construct the analog now.

### Title
Fee calculation in Snowbridge outbound queue can round the ETH-denominated relayer fee to zero even with fully "valid" pricing parameters, allowing underpriced messages to spam relayer processing - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
The external report concerns using a Chainlink price feed getter that can silently return `0` instead of erroring, causing downstream swap math to operate on a zero price and misprice a trade. The direct on-chain analog is `Pallet::<T>::calculate_fee` in the Snowbridge outbound queue: it consumes `PricingParameters` (an "exchange rate" style config value, analogous to the price feed) that pass `PricingParameters::validate()` (all fields non-zero) yet the arithmetic in `calculate_fee` can still silently truncate the ETH-side (`remote`) fee to `0`, exactly like `latestAnswer()` silently returning `0` for a "valid-looking" but effectively broken price/config combination.

### Finding Description
`calculate_fee` computes the remote (Ethereum-side) fee as: [1](#0-0) 

The remote fee is derived through `U256` arithmetic, downcast to `u128` via `defensive_unwrap_or(u128::MAX)`, converted to `FixedU128`, multiplied by `multiplier`, divided by `exchange_rate`, and finally passed through `convert_from_ether_decimals`, which performs an integer division by `10^decimals` to align 18-decimal Ether units with the chain's native decimals (10 or 12): [2](#0-1) 

`PricingParameters::validate()` only checks that `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` are individually non-zero — it never checks that the *resulting computed fee* survives the final integer-division-by-decimals step: [3](#0-2) 

This is confirmed by the pallet's own regression test, which feeds fully non-zero, "validated" pricing parameters and still gets `fee.remote == 0`: [4](#0-3) 

This `fee` value returned by `calculate_fee` is exactly what `SendMessage::validate` uses to determine how much the caller must pay to have a message delivered to Ethereum: [5](#0-4) 

Because `validate` does not re-check that `fee.remote` (or `fee.local`) is non-zero/sane before returning it, an XCM router or the `EthereumBlobExporter`/`pallet_xcm` fee-charging logic downstream will charge the caller `fee.remote = 0` for the Ethereum-side relayer reward and gas refund component, exactly as the deprecated `latestAnswer()` silently returning `0` lets a swap proceed using a broken price rather than reverting.

### Impact Explanation
If governance sets pricing parameters that are individually non-zero (thus passing `validate()`), but combine such that `calculate_fee`'s final integer division truncates `fee.remote` to `0` (as demonstrated by the existing test), then messages can be enqueued to Ethereum without collecting any remote-side fee. Since the remote fee is supposed to fund gas refunds and relayer rewards on Ethereum (per the module's own documented fee formula `RemoteFeeAdjusted = Multiplier * RemoteFee / ExchangeRate`), a persistently zero remote fee means:
- Relayers stop being compensated for delivering messages, which can stall bridge message processing (public underpriced work degrading bridge throughput).
- Users can queue Ethereum-bound messages essentially for free (paying only the trivial local weight fee), enabling spam of the bounded `MessageLeaves`/`Messages` queue up to `MaxMessagesPerBlock`, since nothing in `do_process_message` or `commit` re-validates that a nonzero fee was actually collected before assigning a nonce and committing the message to the merkle root that Ethereum's light client verifies.

### Likelihood Explanation
Low-to-moderate: it requires governance (via `set_pricing_parameters`) to pick parameter combinations that are individually valid per `PricingParameters::validate()` but arithmetically collapse to zero after decimal conversion — the pallet's own test suite proves such combinations exist and are reachable through normal, non-malicious configuration, not through any privileged bypass or malicious actor beyond a governance parameter update using otherwise "valid" inputs.

### Recommendation
Extend `PricingParameters::validate()` (or add a post-computation check in `calculate_fee`) to assert that the final computed `fee.remote` (and `fee.local`) is non-zero after all arithmetic and decimal conversion, returning `InvalidPricingParameters`/rejecting the message in `SendMessage::validate` if the computed fee is zero — mirroring the report's recommendation to use a method that fails loudly (`latestRoundData`) rather than one that silently returns `0` (`latestAnswer`).

### Proof of Concept
The existing unit test already demonstrates the broken invariant end-to-end: [4](#0-3) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1/1` — all individually non-zero and passing `PricingParameters::validate()` — `OutboundQueue::calculate_fee(250000, price_params)` returns `fee.remote == 0`. Wiring this same parameter set through `SendMessage::validate` (`send_message_impl.rs`) would let a message be queued to Ethereum while charging the caller `0` for the remote/relayer-reward portion of the fee.

### Citations

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
