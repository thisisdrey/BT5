### Title
`calculate_fee()` can round the remote (Ethereum-side) fee down to zero without any minimum-bound check, letting users pay a valid-looking but underpriced delivery fee - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The Snowbridge outbound-queue fee logic accepts governance-configured `PricingParameters` and validates only that each field is *non-zero* via `PricingParameters::validate()`. It never checks that the **computed** remote fee actually reflects a reasonable/non-degenerate price after the exchange-rate division and decimal conversion. This mirrors the D3Oracle bug class: a value derived from a price/rate input is accepted purely because the raw inputs are ">0", while the derived output can still fall to zero (or otherwise outside a sane range) due to integer truncation, and no post-computation bound check exists to catch it. [1](#0-0) [2](#0-1) 

### Finding Description
`calculate_fee()` computes the remote fee by:
1. `calculate_remote_fee()` = `fee_per_gas * gas_used_at_most + reward` (in wei).
2. Downcasting to `u128`.
3. Multiplying by `multiplier` and dividing by `exchange_rate` using `FixedU128` fixed-point math.
4. Converting from Ether's 18 decimals to the local chain's decimals via `convert_from_ether_decimals`, which performs an integer division by `10^(18 - local_decimals)`. [3](#0-2) 

`PricingParameters::validate()` — the only guard invoked when governance calls `set_pricing_parameters` — merely asserts each field is non-zero: [1](#0-0) 

Nothing checks that the *result* of the fee formula stays above a sane floor. Because of the two truncating integer divisions (`checked_div(&params.exchange_rate)` on fixed-point inner value, and the final `checked_div(denom)` in `convert_from_ether_decimals`), a fully "valid" (non-zero) set of pricing parameters can still yield `fee.remote == 0`. This is not hypothetical — the repository's own test explicitly demonstrates and comments on this exact outcome: [4](#0-3) 

`calculate_fee()` is invoked directly from the public message-send path `SendMessage::validate()`, which every outbound XCM/Snowbridge message flows through to determine what fee a user must pay to enqueue a message for delivery to Ethereum: [5](#0-4) 

So the wrong ("too low"/zero) price returned by the fee-pricing logic is charged to and accepted from ordinary, unprivileged users sending messages through this pipeline — the parameters themselves are governance-set, but the *acceptance of a degenerate computed fee* is a pure arithmetic/validation gap that any user can trigger merely by sending a message once the parameter combination (fee_per_gas, gas_used, multiplier, exchange_rate, decimals) lands in the truncation zone.

### Impact Explanation
If `fee.remote` truncates to zero (or to a value far below the real gas + reward cost), the outbound-queue pallet still queues and commits the message with a `reward = 0` (or near-zero) for the relayer to claim on the Ethereum side, per the documented fee-settlement formula (`Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward`). Relayers will not be adequately compensated, degrading the incentive for the message to actually be delivered — i.e., public underpriced work that can stall Snowbridge outbound processing, exactly the impact category called out in scope ("public underpriced work that degrades block production or stalls bridge processing"). The local component (`fee.local`) does not compensate for gas paid on Ethereum, so the shortfall is real and can lead to message backlogs / non-delivery once relayers stop finding it profitable to relay.

### Likelihood Explanation
Likelihood is moderate: the vulnerable condition only manifests for specific pricing parameter combinations (as governance sets `exchange_rate`, `fee_per_gas`, `multiplier`), combined with the message's `gas_used_at_most`. However, the flaw is not merely theoretical — it is reproduced by a first-party unit test in the repository showing "the remote fee calculated here is invalid," and confirms no downstream check (in `do_process_message`, `calculate_fee`, or `send_message_impl::validate`) rejects or corrects a resulting zero/degenerate fee. Any combination of governance-set parameters that lands in the truncation regime silently under-prices every message sent through the channel until parameters are manually corrected.

### Recommendation
Add a post-computation sanity check on the *derived* fee before accepting the message, mirroring the Chainlink recommendation to validate output against reasonable bounds rather than only validating raw inputs:
- After computing `fee.remote` in `calculate_fee()`, assert it is above some `MinimumRemoteFee` (e.g., `>= fee_per_gas * gas_used_at_most` at minimum, or a configurable floor), and return an error (`SendError`) from `validate()` if not, rather than silently accepting `0`.
- Alternatively, perform the exchange-rate division and decimal conversion with rounding up (ceiling division) instead of truncating division, so the computed fee never rounds below the true cost.
- Extend `PricingParameters::validate()` to reject exchange-rate/fee_per_gas/multiplier combinations that would make the effective per-gas fee resolve to zero after decimal conversion for any supported `gas_used_at_most` range.

### Proof of Concept
The existing unit test in the repo is itself the PoC, demonstrating that non-zero, "valid" pricing parameters still produce a zero remote fee: [4](#0-3) 

With `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1/1`, and `gas_used = 250000`, `calculate_remote_fee` yields `250001` wei, which after `convert_from_ether_decimals` (dividing by `10^(18-decimals)` for a chain with e.g. 10 or 12 decimals) truncates to `0`. Any user calling into the outbound queue (via XCM export to Ethereum) under these parameters would have their message queued and committed with `reward = 0`, i.e., the "wrong price" is silently accepted with no revert or bound check, exactly analogous to the D3Oracle issue of accepting an out-of-range price because only the ">0" check was performed on the raw input, not the final derived value.

### Citations

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
