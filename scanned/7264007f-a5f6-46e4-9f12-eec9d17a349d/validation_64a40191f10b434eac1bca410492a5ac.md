## Title
Outbound queue fee calculation can silently produce a zero relayer reward for a valid, non-zero-priced message — bridge messages become underpriced/unincentivized to relay - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
This is the closest local analog to the Lido "requested-amount-assumed-equals-received-amount" bug class. In the reported bug, `PufferVault` assumes the amount it locks/accounts for on withdrawal request will equal the amount actually claimable, and never checks or reconciles the discrepancy, leading to a value that is silently "phantom" in accounting. In `snowbridge-pallet-outbound-queue`, `Pallet::<T>::calculate_fee` similarly assumes that a non-zero `PricingParameters` (`exchange_rate`, `fee_per_gas`, `rewards.remote`) will always yield a non-zero remote fee/reward for the relayer, but the ether-decimals conversion step can truncate a genuinely non-zero computed value down to `0` — and this is neither checked nor rejected before the message (with `reward: 0`) is committed for delivery.

## Finding Description
`calculate_fee` computes the remote (Ethereum-side) fee/reward as follows: [1](#0-0) 

The remote fee is first computed in "ether decimals" (`U256`), then downcast to `u128`, scaled by `multiplier`/`exchange_rate` via `FixedU128`, and finally passed through `convert_from_ether_decimals`, which performs an integer division by `10^(ETHER_DECIMALS - T::Decimals)`: [2](#0-1) 

Because `ETHER_DECIMALS` (18) is much larger than the local chain's `Decimals` (10 or 12, enforced by `integrity_test`), the denominator (`10^6` or `10^8`) is large. Any computed fee value smaller than that denominator is truncated to zero by the final integer division — this happens even though every pricing parameter is strictly positive. The pallet's own test explicitly demonstrates and comments on this exact scenario without treating it as an error: [3](#0-2) 

This computed `Fee { local, remote }` (with `remote == 0`) is then used unconditionally in `SendMessage::validate` to quote the price charged to the sender, and in `do_process_message` to set the `reward` field baked into the committed message sent to Ethereum: [4](#0-3) [5](#0-4) 

Neither `validate` nor `do_process_message` verifies that the computed remote fee/reward is non-zero before accepting and committing the message. This mirrors the Lido flaw precisely: a value that is *assumed* to correctly reflect real economic backing (here, "the reward that will actually incentivize a relayer to deliver this message to Ethereum") is accepted and baked into permanent on-chain state (the committed message merkle-proof) without validating it against the real, rounded-down outcome.

## Impact Explanation
A committed message with `reward: 0` gives offchain relayers no incentive to submit it to the Ethereum gateway contract. Per the module's own fee-settlement documentation, on the Ethereum side relayers accrue `Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward`, so a zero reward means the relayer is compensated for gas at best, but the model documented for this bridge is that all normal messages are expected to pay for relaying. Messages that end up systematically zero-reward may be starved of relaying, i.e., legitimate cross-chain XCM messages become stuck, unprocessed at the destination, aligning with "public underpriced work that degrades block production or stalls bridge processing." This does not require a malicious peer/relayer/validator — it is a purely deterministic consequence of the fixed-point truncation for any sender submitting a normal (unprivileged) message once pricing parameters and gas usage fall below the rounding threshold.

## Likelihood Explanation
The likelihood is governed entirely by governance-set `PricingParameters` (exchange rate, fee_per_gas, multiplier, rewards) combined with a message's `gas_used_at_most`. Because `Decimals` is fixed at 10 or 12 (asserted in `integrity_test`) while Ethereum operates at 18 decimals, the rounding denominator is `10^6`–`10^8`; any legitimately small remote-fee output (in the sub-denominator range) triggers this silently, as shown by the pallet's own regression test using entirely valid, non-zero inputs. This is not a contrived edge case — it is directly demonstrated in-repo.

## Recommendation
- In `calculate_fee` (and/or in `SendMessage::validate` and `do_process_message`), explicitly reject (or defensively floor to a minimum) the case where `fee.remote == 0` while any of the pricing parameters (`fee_per_gas`, `rewards.remote`) are non-zero, returning an error (e.g. `SendError::Fee` / a dedicated `Error::<T>::InvalidFee`) instead of silently proceeding.
- Alternatively, use a rounding scheme (round up, or add a minimum floor equal to `1` unit in local decimals) instead of truncating, so the computed fee/reward can never be smaller than the smallest representable non-zero amount, guaranteeing relayers always receive a real economic incentive.
- Add an integration test asserting that `validate`/`do_process_message` reject or floor zero-remote-fee messages under governance-supplied pricing parameters, not just a unit test documenting the current (unfixed) truncation.

## Proof of Concept
Existing regression test in the repository already demonstrates the exact broken invariant (valid non-zero pricing parameters → zero remote fee, accepted without error): [3](#0-2) 

To demonstrate downstream impact, extend this test by calling `OutboundQueue::validate` (or driving a full `do_process_message`) with a `Message` whose `gas_used_at_most`, combined with the above `price_params`, yields `fee.remote == 0`, and observe that:
1. `validate` returns `Ok` and produces a `Fee` with `remote == 0` charged to the sender.
2. `do_process_message` commits a `CommittedMessage` with `reward: 0` into `Messages`/`MessageLeaves`, which is merkleized and exposed via `prove_message`, i.e., permanently accepted into bridge state with no relayer incentive, despite non-zero configured pricing.

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
