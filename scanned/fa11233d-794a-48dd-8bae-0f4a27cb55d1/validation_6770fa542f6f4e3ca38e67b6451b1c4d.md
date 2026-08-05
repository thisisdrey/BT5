### Title
Outbound queue delivery fee can silently round down to zero, letting senders underpay for guaranteed relayer reward - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` in the Snowbridge outbound-queue pallet computes the DOT-denominated delivery fee that a sender must pay to have a message relayed to Ethereum. Just like the `BAMM.sol` bug where `getSwapCollateralAmount` could silently return `0` on a failed price lookup and let the caller pay `thusdAmount` for nothing, `calculate_fee` can silently compute a `remote` fee of `0` even though every `PricingParameters` field is non-zero and passes `PricingParameters::validate()`. No `ensure!`/revert path exists to catch this, so `SendMessage::validate` happily returns a `Fee` whose `remote` component is `0`, and the message is still queued and later committed for cross-chain delivery.

### Finding Description
`calculate_fee` performs: [1](#0-0) 

The intermediate computation truncates via integer/fixed-point arithmetic (`FixedU128::from_inner`, `checked_div`, `convert_from_ether_decimals`). The repository's own test proves that with fully valid, non-zero `PricingParameters` (`exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1/1`), the resulting `fee.remote` is `0`: [2](#0-1) 

`PricingParameters::validate()` only checks that each field is non-zero individually — it cannot catch the case where the *combination* still rounds the derived fee to zero: [3](#0-2) 

`calculate_fee` is called directly from the public message-submission entrypoint `SendMessage::validate`, with no additional guard on the result: [4](#0-3) 

This is the structural analog of the BAMM.sol issue: a price/fee-derivation function that can return `0` under a "valid-looking" input, with no explicit revert/ensure guard at the call site, and the caller-facing amount charged (`Fee::total() = local + remote`) is what downstream billing code uses to charge the sender for the entire delivery, including the relayer reward component that the protocol still promises to pay on the Ethereum side (the `reward` field embedded in the committed message is taken straight from `pricing_params.rewards.remote`, independent of what was actually charged to the sender): [5](#0-4) [6](#0-5) 

### Impact Explanation
When `fee.remote` rounds to `0`, the sender is only billed `fee.local`, yet the committed message still instructs the Ethereum gateway to pay the relayer `Min(GasPrice, MaxFeePerGas) * GasUsed + Reward` (documented at the top of the module) — a nonzero, protocol-guaranteed payout funded from the bridge's own reserves rather than from fees collected from the sender. This is public underpriced work: any unprivileged user submitting a bridge message under parameter combinations that hit this rounding case gets subsidized cross-chain delivery, draining the bridge's fee reserves relative to what it pays relayers, over many submissions. This falls squarely under "public underpriced work that degrades block production or stalls bridge processing" in the impact gate, since sustained underpricing threatens the reserve backing relayer rewards and can eventually stall bridge processing (relayers stop servicing messages that no longer cover their actual cost, or the bridge account is drained).

### Likelihood Explanation
The condition depends purely on governance-set `PricingParameters` (`exchange_rate`, `fee_per_gas`, `rewards.remote`, `multiplier`) combined with the per-message `gas_used_at_most`, all of which pass `validate()`'s non-zero checks yet can still combine — via `saturating_mul`/`checked_div`/integer truncation in `FixedU128` and `convert_from_ether_decimals` — to a `0` result, as directly demonstrated by the existing unit test. Any unprivileged account calling the public message-submission path (e.g. via XCM export to Ethereum) under such parameters triggers the underpayment with no attacker privilege required.

### Recommendation
Add an explicit guard in `calculate_fee` (or immediately after invoking it in `send_message_impl::validate`) that rejects/reverts when the computed `fee.remote` is `0` while `params.rewards.remote` is non-zero, e.g. `ensure!(!fee.remote.is_zero(), Error::<T>::InvalidFee)`, mirroring the BAMM.sol fix of reverting rather than silently proceeding with a zero price-derived value.

### Proof of Concept
The existing regression test in the repository already demonstrates the exact zero-fee condition with fully "valid" non-zero pricing parameters: [2](#0-1) 
Any message submitted while `T::PricingParameters::get()` resolves to values producing this rounding (e.g. small `gas_used_at_most` combined with `exchange_rate`, `fee_per_gas`, and `rewards.remote` all equal to `1`) will pass through `SendMessage::validate` with `fee.remote == 0`, undercharging the sender relative to the reward the protocol still commits to pay on Ethereum.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L280-287)
```rust
impl<Balance> Fee<Balance>
where
	Balance: BaseArithmetic + Unsigned + Copy,
{
	pub fn total(&self) -> Balance {
		self.local.saturating_add(self.remote)
	}
}
```
