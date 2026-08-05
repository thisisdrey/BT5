Audit Report

## Title
Outbound queue fee calculation can silently round the entire Ethereum-side relayer reward/gas fee to zero, allowing underpriced message delivery to Snowbridge - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
`Pallet::<T>::calculate_fee` computes the Ethereum-side relayer fee (`fee.remote`) via `convert_from_ether_decimals`, which performs a plain floor division by a fixed decimals-adjustment denominator (`10^(18 - T::Decimals)`, e.g. `1e8` for a 10-decimal chain). Any legitimately computed wei-denominated fee smaller than this denominator is silently floored to `0`, even though `PricingParameters::validate()` only checks that individual input fields are non-zero and never validates the final computed fee. This lets messages be queued and delivered to Ethereum with zero relayer reward/gas-refund fee collected.

## Finding Description
The vulnerable code path is: [1](#0-0) 

which calls `convert_from_ether_decimals` to floor-divide the fee by a fixed denominator: [2](#0-1) 

`PricingParameters::validate()` only guards that raw inputs (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) are individually non-zero, never that the composed, decimals-converted fee is non-zero: [3](#0-2) 

This is confirmed directly by the pallet's own existing unit test, which reproduces exactly this scenario with individually-valid, non-zero parameters and asserts `fee.remote == 0`: [4](#0-3) 

The computed fee is used unconditionally by `SendMessage::validate`, with no check that `fee.remote` is non-zero before returning a valid ticket: [5](#0-4) 

Tracing the fee usage further, the XCM `EthereumBlobExporter::validate` invokes `OutboundQueue::validate` and converts the total fee (`fee.total()` = `fee.local + fee.remote`) into an `Asset` to be charged by the XCM executor: [6](#0-5) 

The XCM executor's `ExportMessage` handling then charges exactly this fee via `take_fee` before delivering the ticket: [7](#0-6) 

So when `fee.remote` rounds to `0`, the sender is charged only `fee.local` (covering local weight/processing cost) with nothing collected to cover the relayer's Ethereum-side gas refund and reward, and the message still proceeds to `deliver` and is enqueued for relay to Ethereum.

## Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" impact category. The corrupted value is `fee.remote` (the Ethereum-side gas-refund/reward component of `Fee<T::Balance>` returned by `calculate_fee`), which silently becomes `0` instead of reflecting the actual relayer cost. Messages with zero remote fee are still accepted into the outbound queue and committed for delivery to Ethereum, but relayers have no economic incentive to deliver them, which can stall processing of that channel's outbound messages to Ethereum.

## Likelihood Explanation
No privileged or malicious behavior is required — any ordinary combination of `PricingParameters` (each field individually non-zero, as enforced by `validate()`) combined with a small `gas_used_at_most` for a given command can push the pre-conversion wei fee below the `10^(18 - T::Decimals)` floor, deterministically zeroing `fee.remote`. This is reproduced by the repository's own existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`, confirming the bug is real, repeatable, and already known-but-unfixed in this codebase.

## Recommendation
- Change `convert_from_ether_decimals` to round up (ceiling division) instead of truncating, so any non-zero pre-conversion fee never collapses to `0`.
- Extend `PricingParameters::validate` (or add a check inside `calculate_fee`) to reject configurations for which the computed `fee.remote` would be `0` for the minimum supported `gas_used_at_most`.
- Add a defensive check in `SendMessage::validate` to return `SendError` if `fee.remote == 0` while `pricing_params.rewards.remote != 0`, preventing underpriced messages from entering the outbound queue.

## Proof of Concept
The existing repository test demonstrates the defect directly:
```rust
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
assert_eq!(fee.remote, 0); // non-zero, validate()-passing params yield zero remote fee
``` [4](#0-3) 

This message would still pass `SendMessage::validate` and `deliver`, be enqueued via `T::MessageQueue::enqueue_message`, and committed for relay to Ethereum with zero relayer reward/gas-refund fee collected.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L129-137)
```rust
		let (ticket, fee) = OutboundQueue::validate(&outbound_message).map_err(|err| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		// convert fee to Asset
		let fee = Asset::from((Location::parent(), fee.total())).into();

		Ok(((ticket.encode(), message_id), fee))
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1697-1711)
```rust
				let (ticket, fee) = validate_export::<Config::MessageExporter>(
					network,
					channel,
					universal_source,
					destination.clone(),
					xcm,
				)?;
				self.transactional_process(|self_ref| {
					self_ref.take_fee(fee, FeeReason::Export { network, destination })?;
					let _ = Config::MessageExporter::deliver(ticket).defensive_proof(
						"`deliver` called immediately after `validate_export`; \
						`take_fee` does not affect the validity of the ticket; qed",
					);
					Ok(())
				})
```
