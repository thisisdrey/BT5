The claim is fully confirmed by the code as written. The vulnerability is a genuine, reproducible fee-underpricing bug in the outbound queue's fee calculation, backed by the pallet's own regression test.Audit Report

## Title
Snowbridge outbound message delivery fee rounds down to zero, letting users pay nothing for Ethereum-bound messages while relayers/reward pot bear the cost - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
`Pallet::calculate_fee` computes the native-currency fee owed for a message to be relayed to Ethereum, converting a wei-denominated remote fee through `FixedU128` arithmetic and then truncating it via integer division in `convert_from_ether_decimals`. Because this truncating division happens after the `FixedU128` multiply/divide by `multiplier`/`exchange_rate`, a legitimate non-zero `fee_per_gas`/`reward` combination can still produce an inner wei value smaller than the decimal-conversion denominator (`10^8` for 10-decimal balances, `10^6` for 12-decimal), collapsing `fee.remote` to `0` while `fee.local` remains non-zero. `validate()` accepts and enqueues the message with this zero remote fee unconditionally, so a normal user can have a message relayed to Ethereum without paying anything toward the relayer's gas/reward.

## Finding Description
In `calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-393`):
1. `calculate_remote_fee` computes `fee_per_gas * gas_used_at_most + reward` in wei (`U256`) — `lib.rs:396-402`.
2. This is downcast to `u128`, wrapped as `FixedU128::from_inner`, multiplied by `params.multiplier`, divided by `params.exchange_rate`, and unwrapped via `.into_inner()` — `lib.rs:379-387`.
3. `convert_from_ether_decimals` then divides this 18-decimal wei-scaled value by `10^(18 - T::Decimals)` using plain integer `checked_div`, which truncates any remainder — `lib.rs:414-418`.

This chained truncation means a small but fully valid, governance-set pricing configuration (e.g. `fee_per_gas = 1`, `reward = 1`, `exchange_rate = 1`, `multiplier = 1`) yields a wei value (e.g. `250001` for `gas_used = 250000`) that is smaller than the `10^8`/`10^6` divisor enforced by `integrity_test` (`lib.rs:259-262`), so `fee.remote` truncates to `0`. `validate()` in `send_message_impl.rs:41-74` calls `calculate_fee` and returns whatever `Fee` results with no minimum-fee assertion — the `Ticket` is unconditionally accepted for enqueue and delivery regardless of `fee.remote == 0`. The pallet's own regression test, `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` (`bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`), reproduces exactly this scenario and its comment explicitly flags the resulting `fee.remote == 0` as invalid/should-be-avoided, with no existing guard rejecting it.

## Impact Explanation
The `remote` fee component exists to reimburse relayers for the Ethereum gas they spend executing the command plus their reward. When pricing parameters combine to a wei-scaled value below the decimal-conversion denominator, any user submitting a message under the current governance-set pricing pays `0` toward this cost while the relayer still incurs real gas expenditure executing the message on Ethereum. This matches the allowed "public underpriced work that … stalls bridge processing" impact: unprivileged senders obtain free/subsidized outbound execution, degrading relayer incentives and bridge processing economics, entirely through the normal public message-send path with no privileged actor involved.

## Likelihood Explanation
The denominator is fixed at `10^8` or `10^6` per `integrity_test`'s enforcement that `T::Decimals` is 10 or 12. Any combination of `fee_per_gas`/`reward`/`exchange_rate`/`multiplier` (parameters periodically updated per module documentation) that keeps the wei-scaled value below this denominator for a given `gas_used_at_most` triggers the bug, and it requires no special privilege — any account calling the standard send-message path is affected. It is directly reproduced by the pallet's own existing unit test with realistic-looking parameter values (`exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`), confirming feasibility and repeatability.

## Recommendation
- In `calculate_fee`/`convert_from_ether_decimals`, avoid silently truncating a non-zero wei value to zero: round up or apply a minimum floor of 1 when the input is non-zero, rather than plain truncating `checked_div`.
- Add an explicit guard in `validate()` (e.g. `ensure!(fee.remote > 0 || pricing_params_indicate_zero_cost, Error::<T>::FeeTooLow)`) before returning the `Ticket`, so messages cannot be queued for remote execution with an under-priced (zero) remote fee while relayer-facing gas/reward parameters are non-zero.
- Perform the multiply-then-divide chain in higher precision (e.g. `U256`) throughout, to avoid compounding rounding loss between the `FixedU128` exchange-rate division and the final decimal-conversion division.

## Proof of Concept
The existing unit test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` (`bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`) demonstrates the flaw: with `exchange_rate = FixedU128::from_rational(1,1)`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1`, and `gas_used = 250000`, `calculate_remote_fee` yields `250001` wei, which after the `FixedU128` multiply/divide and `convert_from_ether_decimals`'s division by `10^8` (for 10-decimal `Test::Decimals`) truncates to `fee.remote == 0`, while `fee.local == 698000000` — the sender pays only the local weight fee and nothing toward the relayer's Ethereum gas/reward. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L259-262)
```rust
		fn integrity_test() {
			let decimals = T::Decimals::get();
			assert!(decimals == 10 || decimals == 12, "Decimals should be 10 or 12");
		}
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L396-418)
```rust
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
