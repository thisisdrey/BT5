## Analysis

The reported bug class — `Number(x).toFixed(2)` silently rounding a small positive value down to `0`, hiding real value from the caller — has a direct structural analog in Snowbridge's outbound message fee computation.

### Local Analog Found

`Pallet::<T>::calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` computes the fee a user must pay to have a message relayed to Ethereum, split into a `local` and `remote` component: [1](#0-0) 

The `remote` component is derived by taking a `U256` ether-denominated fee, downcasting to `u128`, applying the `multiplier`/`exchange_rate` via `FixedU128`, and finally dividing by a decimal-adjustment `denom` in `convert_from_ether_decimals`: [2](#0-1) 

This integer division truncates any remainder smaller than `denom` down to `0`, even though the true remote fee (which is supposed to cover the relayer's gas refund + reward on Ethereum) is strictly positive. This is confirmed by the pallet's own regression test: [3](#0-2) 

The test explicitly documents: *"Though non[e] zero pricing params the remote fee calculated here is invalid which should be avoided."* — i.e. the maintainers themselves flag this as an unresolved defect, not intended behavior.

### Why Existing Guards Don't Stop It

`SendMessage::validate` in `send_message_impl.rs` calls `calculate_fee` and returns whatever `Fee{local, remote}` comes out, with **no check that `remote > 0`**: [4](#0-3) 

Any unprivileged user who sends a message via the bridge (from a sibling parachain via XCM, or the system pallet) is charged whatever `calculate_fee` returns — including `remote = 0` — and the message is still accepted and enqueued for relay to Ethereum. There is no lower-bound/minimum-fee enforcement analogous to `assets/tx-payment`'s `!fee.is_zero()` guard pattern seen elsewhere in the codebase (e.g. `substrate/frame/transaction-payment/asset-tx-payment/src/tests.rs` documents exactly this guard for a structurally identical truncation risk, but no such guard exists here).

### Title
Outbound Queue `calculate_fee` Can Round a Non-Zero Remote (Ethereum) Delivery Fee Down to Zero, Allowing Underpriced Bridge Messages - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee`/`convert_from_ether_decimals` uses plain integer division (`checked_div`) with no rounding-up or minimum-fee enforcement when converting the ether-denominated remote fee into native currency. For legitimate, non-degenerate `PricingParameters` (non-zero `fee_per_gas`, `exchange_rate`, `multiplier`, `reward`), the computed `remote` fee can still truncate to `0`, as demonstrated by the pallet's own test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`.

### Finding Description
The remote fee is meant to fund the relayer's gas refund and reward on the Ethereum Gateway contract side (`RemoteFee(Message) = MaxGasRequired * MaxFeePerGas + Reward`, per the module docs). After conversion through `FixedU128` scaling and `convert_from_ether_decimals`'s integer division by `10^(18 - T::Decimals)`, small computed values are truncated to `0`. `send_message_impl.rs::validate` does not reject or clamp a zero/underpriced `remote` fee before returning the `Ticket`/`Fee` pair, and `deliver` unconditionally enqueues the message once validated.

### Impact Explanation
This is public underpriced work with direct bridge-processing impact: a normal user can submit (and have accepted) Ethereum-bound messages while paying `0` toward the remote/relayer-reward component. Since relayers are only reimbursed based on funds actually collected and forwarded to the reward/incentive accounting, systematically zero-priced remote fees degrade the economic incentive for relayers to deliver messages, which can stall outbound bridge processing (messages queue up with no relayer compensation) — matching the explicit "public underpriced work that ... stalls bridge processing" impact category.

### Likelihood Explanation
No privileged actor is required — any account able to send an XCM message through a registered channel to the bridge (or use the system pallet's `send`) can trigger `calculate_fee` with governance-set `PricingParameters` that are valid/non-degenerate but happen to produce a sub-`denom` remote fee for the given `gas_used_at_most`. Because parameters are periodically updated by governance for market conditions rather than to avoid this arithmetic edge case, this can occur under ordinary, non-malicious configurations, especially for low-gas commands.

### Recommendation
Apply rounding-up (ceiling) division instead of floor division in `convert_from_ether_decimals`/the overall `calculate_fee` pipeline, or add an explicit `ensure!(!fee.remote.is_zero(), ...)`/minimum-fee floor check in `calculate_fee` or `SendMessage::validate`, rejecting message submission (or bumping to a minimum unit) rather than silently accepting a zero-priced remote fee.

### Proof of Concept
The existing unit test already reproduces the condition: [3](#0-2) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1/1`, and `gas_used = 250000`, `calculate_fee` returns `fee.remote == 0` while `fee.local == 698000000`, despite all pricing parameters being non-zero/valid. Since `send_message_impl.rs::validate` performs no check on `fee.remote`, such a message would be validated and enqueued for delivery to Ethereum with zero compensation reserved for the relayer's remote-side costs.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-73)
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
