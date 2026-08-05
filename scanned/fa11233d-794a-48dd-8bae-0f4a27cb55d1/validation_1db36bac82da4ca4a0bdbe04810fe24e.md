Given the tool-call budget is exhausted, here is my analysis based on the evidence I was able to gather from `bridges/snowbridge/pallets/outbound-queue`.

### Title
Snowbridge outbound-queue relayer fee can round down to zero, allowing underpriced message delivery to stall bridge processing - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
This is a local analog of the Bond protocol rounding-direction bug: just as `_currentMarketPrice` rounded down where the specification required rounding up (letting makers be shortchanged), Snowbridge's `calculate_fee` chain performs multiple integer divisions that always round down, with no floor/minimum enforced on the resulting relayer `reward`/remote fee component. This lets the fee charged to the sender legitimately compute to `0` even though `PricingParameters` are non-zero, meaning the protocol accepts and commits a message for Ethereum delivery while collecting no payment to cover the relayer's on-chain reward.

### Finding Description
`Pallet::<T>::calculate_fee` computes the remote (Ethereum-side) fee via a chain of down-rounding operations: [1](#0-0) 

`calculate_remote_fee` combines `fee_per_gas * gas_used_at_most + reward` (safe), but the subsequent `FixedU128` division by `exchange_rate` and the final `convert_from_ether_decimals` both truncate: [2](#0-1) 

Every step here rounds down: the `FixedU128::checked_div` by the exchange rate discards the fractional remainder, and `convert_from_ether_decimals` performs `value.checked_div(denom)` which truncates when the ether-denominated fee (18 decimals) is scaled down to the chain's native decimals (10 for DOT, 12 for KSM). Unlike `BondBaseSDA.marketPrice` which rounds up to protect the "maker" (here, the relayer who must be reimbursed), this path has no rounding-up/ceiling anywhere, and no post-computation floor check that `fee.remote > 0`. The existing unit test confirms the resulting remote fee can be exactly `0` even with valid, non-zero pricing parameters: [3](#0-2) 

This fee is charged through the unprivileged, public `SendMessage::validate` entrypoint (reachable by any account sending assets/XCM to Ethereum), which calls `calculate_fee` directly and returns the computed `Fee` to be paid by the caller, then enqueues the message unconditionally: [4](#0-3) 

No check anywhere in `validate`, `deliver`, or `calculate_fee` rejects a message whose computed remote fee is zero.

### Impact Explanation
This falls under the explicitly accepted impact category "public underpriced work that degrades block production or stalls bridge processing." A message can be queued and committed into the merkle-root digest for Ethereum delivery (consuming `MessageLeaves` capacity, weight, and nonce sequence) while the relayer economic reward component is zero. Because Ethereum-side relayers are only compensated per the `reward`/gas-refund model documented in the module (`Min(GasPrice, MaxFeePerGas) * GasUsed + Reward`), a zero-reward message provides no incentive for delivery. Given the strictly incrementing per-channel `Nonce` used for ordering/replay protection, an accumulation of such underpriced messages can create a backlog that stalls processing of subsequent, correctly-priced messages on the same channel, degrading bridge throughput without requiring any privileged actor.

### Likelihood Explanation
Likelihood depends on realistic pricing-parameter magnitudes (`fee_per_gas`, `exchange_rate`, `reward`) set by governance, which are not directly attacker-controlled. However, an attacker can choose command types with minimal `gas_used_at_most` and can act during periods where the ETH/native exchange rate or decimal-scaling combination pushes the computed fee toward the truncation boundary — this is a systemic rounding defect, not a one-off misconfiguration, so it will recur any time the computed remote fee before truncation is fractional and less than 1 unit of native currency. I was not able to fully verify from this repo alone whether the Ethereum gateway contract strictly enforces in-order nonce processing (which would confirm the full "stall" severity); this detail lives in the off-chain/Ethereum contract code, outside the indexed Rust codebase, so I flag this as unresolved.

### Recommendation
Round the remote-fee conversion steps up (ceiling division) instead of down, consistent with the principle that fees must be sufficient to cover the promised relayer reward, and additionally enforce a minimum non-zero floor on `fee.remote` in `calculate_fee` before returning it to `SendMessage::validate`, rejecting or re-pricing messages that would otherwise be committed with an underpriced/zero relayer incentive.

### Proof of Concept
Using the existing test harness pattern in `bridges/snowbridge/pallets/outbound-queue/src/test.rs`, the test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` already demonstrates the exact primitive: [3](#0-2) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, and `gas_used = 250000`, `fee.remote` resolves to `0` while `fee.local` is non-zero — i.e., the message is accepted and queued for Ethereum delivery, but the on-chain relayer-incentive component collected is zero. An attacker submitting messages under similarly unfavorable (but governance-plausible) parameter/decimal combinations via `SendMessage::validate` → `deliver` can repeat this to enqueue underpriced messages that no rational relayer will deliver, without needing any privileged role.

**Note on completeness:** I was unable to load `bridges/snowbridge/primitives/core/src/pricing.rs` (to confirm whether `PricingParameters` has any built-in validity/minimum-fee constraints) and `outbound-queue-v2` fee logic in the final iteration due to tool errors, so I cannot fully rule out an existing mitigation in those files. If such a floor exists there, it should be verified and, if absent, added per the recommendation above.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L395-418)
```rust
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
