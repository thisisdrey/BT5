Audit Report

## Title
Zero-value remote fee bypasses relayer reward/gas-refund charge in Snowbridge outbound queue - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
`Pallet::<T>::calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` computes the ETH-denominated `remote` fee via `checked_div`/downcast fixed-point math and a final truncating integer division in `convert_from_ether_decimals`, which can silently round the relayer's gas-refund/reward fee to `0` even when all `PricingParameters` are individually validated as non-zero. The zero value is returned unguarded from `calculate_fee` and consumed directly by `SendMessage::validate` in `send_message_impl.rs`, meaning an attacker-controlled low-gas `Command` can bypass the relayer reward/gas-refund charge entirely. [1](#0-0) [2](#0-1) 

## Finding Description
`calculate_fee` computes `fee = fee_per_gas * gas_used_at_most + reward`, converts it via `FixedU128` multiplier/exchange-rate division, and then calls `convert_from_ether_decimals`, which performs `value.checked_div(denom)` — plain truncating integer division — with no rounding-up or minimum-floor logic. [3](#0-2)  Because `denom = 10^(ETHER_DECIMALS - T::Decimals)` (e.g., `10^8` for DOT), any intermediate wei value smaller than `denom` truncates to exactly `0`. `PricingParameters::validate` only checks that `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` are individually non-zero as *inputs* — it provides no guarantee about the derived per-message `remote` fee after all arithmetic and decimal truncation. [4](#0-3)  The pallet's own test explicitly reproduces this exact scenario with fully valid, non-zero pricing parameters, confirming `fee.remote == 0` is reachable and was known to maintainers, with the test comment stating this outcome "should be avoided."

The zero `remote` fee flows unguarded into `send_message_impl::validate`, which calls `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())` and returns the resulting `Fee` with no check that `fee.remote != 0`. [5](#0-4)  This fee (`fee.total()` = `local + remote`) is subsequently used by `EthereumBlobExporter::validate` as the `Asset` amount withdrawn from the message sender via XCM `BuyExecution`. [6](#0-5)  Since `gas_used_at_most` derives from `T::GasMeter::maximum_gas_used_at_most(&message.command)`, and `command` is attacker-supplied XCM content parsed by `XcmConverter::convert`, an unprivileged user can select commands landing in the truncation-to-zero regime, causing the relayer's ETH-side reward/gas-refund component to collapse to zero while the message is still accepted and queued for delivery.

## Impact Explanation
This matches the "public underpriced work that degrades... stalls bridge processing" clause of the impact gate: an unprivileged external user can submit XCM messages via the public `EthereumBlobExporter`/`SendMessage::validate` path that result in `Fee.remote == 0`, meaning relayers receive no ETH-side compensation for delivering that message to Ethereum. Over time or at scale, this degrades relayer incentive to process such messages, potentially stalling bridge message delivery for the affected command types, without requiring any privileged, malicious-node, or leaked-key assumption.

## Likelihood Explanation
The precondition depends on the currently configured `PricingParameters` (`fee_per_gas`, `rewards.remote`, `exchange_rate`, `multiplier`, `T::Decimals`) combined with the minimum `gas_used_at_most` for a given command. Whether this is currently exploitable depends on the live production values of these governance-set parameters — the repository's own regression test demonstrates the code path is reachable with plausible non-zero values (`fee_per_gas = 1`, `reward = 1`, `gas_used = 250000` on a 10-decimal chain), and no runtime guard prevents such a configuration from existing or persisting.

## Recommendation
In `convert_from_ether_decimals` and/or `calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs`), use round-up division (e.g., `value.div_ceil(denom)` or add `denom - 1` before dividing) instead of truncating `checked_div`, and/or enforce a minimum non-zero floor on the computed `remote` fee. Alternatively, reject message validation in `send_message_impl::validate` when `fee.remote.is_zero()` so that no message can be enqueued without a guaranteed non-zero relayer reward/gas-refund.

## Proof of Concept
The existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` demonstrates the bug directly: with `exchange_rate = 1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}`, `multiplier = 1`, and `gas_used = 250000`, `OutboundQueue::calculate_fee` returns `Fee { local: 698000000, remote: 0 }`. [7](#0-6)  Since `send_message_impl::validate` calls `calculate_fee` with these exact runtime-configured `PricingParameters` and returns the result without any zero-check, any XCM message routed through `EthereumBlobExporter` with a low-gas command under this configuration will be accepted with `remote = 0`, and the total fee charged to the sender excludes any relayer reward.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L58-73)
```rust

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
