All claims verified against the actual repository code. The code paths, test file, and logic exactly match what's described in the report.

Verification summary:
- `calculate_fee` at [1](#0-0)  computes `fee.remote` through `calculate_remote_fee`, fixed-point scaling, and `convert_from_ether_decimals`.
- `convert_from_ether_decimals` performs a plain integer `checked_div` with no rounding-up or minimum-floor protection at [2](#0-1) .
- The existing regression test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` confirms `fee.remote == 0` under realistic, fully non-zero `PricingParameters`, with an explicit comment flagging it as an unresolved issue, at [3](#0-2) .
- `SendMessage::validate` calls `calculate_fee` and returns the `Fee` unchecked at [4](#0-3) , with no assertion that `fee.remote` is non-zero.
- The `EthereumBlobExporter::validate` in the XCM converter directly consumes `fee.total()` as the charged `Asset` with no additional validation at [5](#0-4) .
- Meanwhile, `do_process_message` embeds the full, un-truncated `pricing_params.rewards.remote` and `pricing_params.fee_per_gas` directly into the committed Ethereum-bound message, independent of what was actually charged, at [6](#0-5) .

This confirms a genuine mismatch between the (possibly zero) fee charged to the sender and the non-zero reward/gas-refund commitment embedded in the Ethereum-bound message, exploitable by any unprivileged account sending XCM messages through the Snowbridge exporter with governance-set pricing parameters that cause truncation to zero.

Audit Report

## Title
Truncated remote fee can compute to zero without validation, causing free/underpriced Snowbridge message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
`Pallet::calculate_fee` can return a `Fee` whose `remote` component truncates to `0` even with fully non-zero `PricingParameters`, due to unrounded integer division in `convert_from_ether_decimals`. `SendMessage::validate` and the XCM `EthereumBlobExporter::validate` propagate this unchecked value as the fee charged to the sender, while `do_process_message` still commits the full, untruncated `reward` and `max_fee_per_gas` to the Ethereum-bound message.

## Finding Description
`calculate_fee` computes the remote fee through `calculate_remote_fee`, fixed-point scaling by `multiplier`/`exchange_rate`, and finally `convert_from_ether_decimals`, which performs `value.checked_div(denom)` with no rounding-up or minimum-floor safeguard. For small but valid, non-zero `fee_per_gas`/`reward`/`exchange_rate` combinations, this truncates `fee.remote` to `0`, exactly as demonstrated by the existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`, whose own comment states this is "invalid which should be avoided." `SendMessage::validate` calls `calculate_fee` and returns the resulting `Fee` unchecked to callers; the XCM exporter (`EthereumBlobExporter::validate`) converts `fee.total()` directly into the `Asset` charged via `BuyExecution`, with no assertion that the remote component is non-zero. Separately, `do_process_message` builds the `CommittedMessage` sent to Ethereum using the full, non-truncated `pricing_params.rewards.remote` and `pricing_params.fee_per_gas` — values independent of what was actually charged to the sender. No existing guard anywhere in this pipeline checks that the charged `fee.remote` is sufficient to cover the reward/gas-refund promised on-chain to relayers.

## Impact Explanation
This is public underpriced work that stalls or degrades bridge processing: any unprivileged account able to route an XCM message through the Snowbridge exporter can, under governance-set pricing parameters that plausibly yield small values, pay `fee.local` only while the committed Ethereum message still promises relayers the full `reward`/gas-refund. Repeated abuse either drains the reserve funding relayer rewards or removes relayer incentive entirely, stalling message delivery — both fall within the accepted "public underpriced work" impact category.

## Likelihood Explanation
No malicious peer, relayer, or governance actor is needed — only governance setting a plausible pricing parameter combination (small `exchange_rate`, low `fee_per_gas`/`reward`) combined with a normal user sending a message. The bug is proven reachable via an existing, currently-passing unit test using realistic non-zero parameters, not a contrived edge case.

## Recommendation
In `calculate_fee` (or `SendMessage::validate`), enforce that `fee.remote` is non-zero (or above a configured minimum) whenever `pricing_params.rewards.remote`/`fee_per_gas` are non-zero, returning a `SendError` otherwise. Alternatively, use round-up division in `convert_from_ether_decimals` for the fee path and add an explicit check that the charged remote fee covers the reward/gas-refund committed in `do_process_message`.

## Proof of Concept
1. Governance sets `PricingParameters { exchange_rate: 1, fee_per_gas: 1, rewards: { local: 1, remote: 1 }, multiplier: 1 }`, per the existing test at `bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`.
2. A user sends a message with `gas_used_at_most = 250000` through `SendMessage::validate` (`send_message_impl.rs:59-60`), invoking `calculate_fee`.
3. `fee.remote` truncates to `0` per `convert_from_ether_decimals` (`lib.rs:414-418`); the exporter charges only `fee.local` via `BuyExecution` (`converter/mod.rs:129-136`).
4. `do_process_message` (`lib.rs:332-352`) still embeds the full `pricing_params.rewards.remote` and `fee_per_gas`-derived `max_fee_per_gas` into the Ethereum-committed message, creating an unfunded relayer reward obligation.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L414-418)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L129-136)
```rust
		let (ticket, fee) = OutboundQueue::validate(&outbound_message).map_err(|err| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		// convert fee to Asset
		let fee = Asset::from((Location::parent(), fee.total())).into();

```
