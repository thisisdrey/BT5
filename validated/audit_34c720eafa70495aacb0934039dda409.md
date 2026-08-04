### Title
Underpriced remote-fee rounding to zero in Snowbridge `pallet-outbound-queue::calculate_fee` allows fee-free message export - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The external report's core broken invariant is: a per-unit rate conversion (`amount * rate / denominator`) can round down to zero for a valid, non-zero input, and the surrounding protocol does not guard against that zero outcome, defeating the economic assumption that every unit of work must be paid for. In `pallet-outbound-queue` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs`), `calculate_fee` performs the analogous chain of multiplications/divisions to price a message for Ethereum delivery, and it can legitimately return `remote = 0` even with non-zero `exchange_rate`, `fee_per_gas`, and `reward` — a fact the pallet's own test suite documents but does not prevent.

### Finding Description
`calculate_fee` computes the fee owed for exporting a message to Ethereum: [1](#0-0) 

The remote fee is first computed in wei-scale Ether (18 decimals) via `calculate_remote_fee`, then adjusted with `multiplier` and divided by `exchange_rate`, then rescaled to the local currency's decimals with `convert_from_ether_decimals`: [2](#0-1) 

`convert_from_ether_decimals` divides by `10^(ETHER_DECIMALS - T::Decimals)` (e.g. `10^8` for a 10-decimal chain like Polkadot, `10^6` for Kusama). If the intermediate wei-scale fee produced by the exchange-rate division is smaller than this denominator, integer division truncates the result to `0` — even though every input parameter (`exchange_rate`, `fee_per_gas`, `reward`, `multiplier`) is non-zero and individually valid. The pallet's own test demonstrates this exact condition and explicitly calls the outcome invalid: [3](#0-2) 

Unlike the Party crowdfund's `_processContribution`, which reverts the whole transaction when the rounded value hits zero, `calculate_fee` has no equivalent guard: it silently returns `Fee { remote: 0, .. }`. The caller (`send_message_impl.rs`, invoked by the XCM exporter path) charges whatever `calculate_fee` returns and proceeds to commit the message into `MessageLeaves`/`Messages` storage in `do_process_message`, which is unconditionally executed once a message clears the `MessageQueue`: [4](#0-3) 

Because the fee computation is a pure function of governance-set `PricingParameters` (exchange rate, fee-per-gas, reward, multiplier) combined with the message's `gas_used_at_most`, any unprivileged user who can predict or observe the current pricing parameters (all of which are public storage) can craft/trigger a low-gas message whose computed remote fee rounds to zero, and have it committed into the outbound queue merkle root essentially fee-free. This is not a governance-abuse or parameter-misconfiguration issue — it is a rounding defect present for a wide, foreseeable range of legitimate parameter values (the test uses a plausible 1:1 exchange rate and `fee_per_gas = 1`), so it is reachable purely through normal public message submission without any privileged actor.

### Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" impact category. `pallet-outbound-queue` exists specifically to ensure relayers are compensated for the gas cost of delivering messages to Ethereum and to prevent spam ("The XCM bridge-router on AH will charge a small fee to prevent spamming BH with bridge messages" — see the analogous v2 design doc). A rounding-to-zero remote fee breaks this invariant: an attacker can flood the outbound queue with messages that cost nothing to deliver, causing either (a) relayers to stop servicing the channel (since delivery is unprofitable, messages pile up and the bridge stalls), or (b) if relayers deliver anyway, the bridge's economic security model is defeated and delivery costs are socialized. Both outcomes degrade or stall bridge message processing, which is explicitly in-scope.

### Likelihood Explanation
`PricingParameters` (`exchange_rate`, `fee_per_gas`, `rewards.remote`, `multiplier`) are public runtime storage, so any user can read them and select (or wait for) values/messages for which the computed fee rounds to zero — no special access or malicious relayer/validator/governance action is required. The pallet's own regression test independently confirms the rounding-to-zero output arises for realistic, non-degenerate parameter values, showing this is not a purely theoretical corner case.

### Recommendation
Add an explicit floor/guard in `calculate_fee` (and equivalently in the v2 `outbound-queue-v2` reward path) so that a computed `remote` fee of `0` is rejected or replaced with a `MinimumRelayerReward`/existential-deposit-equivalent floor, analogous to enforcing `votingPower != 0` in the referenced report — but by rejecting/flooring rather than reverting a state transition that other users depend on, to avoid introducing an equivalent DoS on message submission itself. Concretely: after `convert_from_ether_decimals`, `ensure!(fee.remote >= T::MinimumRemoteFee::get(), Error::<T>::FeeTooLow)` or saturate to a configured minimum.

### Proof of Concept
The existing repository test already demonstrates the defect end-to-end at the calculation layer: [3](#0-2) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1`, and `gas_used = 250000`, `calculate_fee` returns `fee.remote == 0` while `fee.local` remains non-zero (`698000000`). This shows a message can be scheduled/committed while the remote-delivery component of its fee is exactly zero, meaning the relayer incentive/anti-spam charge for Ethereum-side delivery is bypassed even though the pallet doc explicitly states this fee exists "to prevent spamming" and to compensate relayers (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:38-67`). A background agent should trace `send_message_impl.rs` to confirm whether a zero `remote` fee is deducted/enforced anywhere before `do_process_message` commits the message, to fully confirm exploitability end-to-end in the current message-submission flow.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-330)
```rust
		/// Process a message delivered by the MessageQueue pallet
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);

			// Decode bytes into versioned message
			let versioned_queued_message: VersionedQueuedMessage =
				VersionedQueuedMessage::decode(&mut message).map_err(|_| Corrupt)?;

			// Convert versioned message into latest supported message version
			let queued_message: QueuedMessage =
				versioned_queued_message.try_into().map_err(|_| Unsupported)?;

			// Obtain next nonce
			let nonce = <Nonce<T>>::try_mutate(
				queued_message.channel_id,
				|nonce| -> Result<u64, ProcessMessageError> {
					*nonce = nonce.checked_add(1).ok_or(Unsupported)?;
					Ok(*nonce)
				},
			)?;
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-393)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L404-418)
```rust
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
