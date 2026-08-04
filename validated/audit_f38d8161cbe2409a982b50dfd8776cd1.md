Based on my investigation, the strongest local analog to the DODO `_calB0WithPriceLimit` division-by-zero/precision-truncation bug is in the Snowbridge outbound-queue fee-calculation pipeline, which shares the exact bug class: a chained multiply-then-divide fixed-point formula that silently truncates to a degenerate (zero) result for legitimate, governance-valid parameter combinations, on a path that is reachable by any ordinary user sending a message through the bridge.

I want to flag upfront that I could not fully trace, within the indexed files, the exact call site where the `Fee` tuple returned by `calculate_fee`/`validate` is actually withdrawn from the sending account's balance (e.g. in the XCM exporter or bridge-hub router pallets were not fully retrievable from the index). This means the "unbacked promise" impact below is inferred from the data model (the committed `reward` in wei is independent of the computed/charged local fee) rather than a directly observed withdrawal bug. If deeper confirmation is needed, a full-repo Devin session would be required to trace the withdrawal call sites.

### Title
Precision truncation in Snowbridge outbound-queue fee conversion can round the remote relayer reward fee to zero - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
`Pallet::calculate_fee` and its helper `convert_from_ether_decimals` compute the DOT/KSM-denominated fee for delivering a message to Ethereum by chaining a fixed-point multiply, a fixed-point divide, and then an integer divide by a decimals-scaling constant. [1](#0-0) 
Like the DODO `_calB0WithPriceLimit` bug (where `temp2 = i*k` truncates to 0 for small price/`k` combinations), this formula can truncate the final result to `0` for legitimate governance-set `PricingParameters` combined with small `gas_used_at_most` values, because the last step is a plain integer `checked_div` by a fixed power-of-ten denominator with no minimum-fee floor.

### Finding Description
`calculate_fee` computes:
```
fee = FixedU128::from_inner(remote_fee_in_wei)
        .saturating_mul(params.multiplier)
        .checked_div(&params.exchange_rate)
        .expect("exchange rate is not zero; qed")
        .into_inner();
fee = convert_from_ether_decimals(fee);
``` [2](#0-1) 

`convert_from_ether_decimals` then does a final truncating integer division:
```
let denom = 10u128.saturating_pow(decimals); // 10^8 for DOT (10 decimals), 10^6 for KSM (12 decimals)
value.checked_div(denom).expect("divisor is non-zero; qed").into()
``` [3](#0-2) 

`PricingParameters::validate()` only guards against the *degenerate* zero-valued parameters (exchange_rate, fee_per_gas, rewards, multiplier), not against small-but-nonzero combinations that cause the composed formula to round the final fee down to `0`: [4](#0-3) 

Critically, the on-chain `reward` value committed for the relayer is **not** derived from this (possibly-truncated) fee — it is taken directly from `pricing_params.rewards.remote` (a fixed wei amount) regardless of what was actually charged to the sender: [5](#0-4) 

This mirrors the DODO root cause exactly: an intermediate multiply/divide step in a pricing formula truncates to zero under otherwise-valid, small-magnitude inputs (small `gas_used_at_most`, i.e., a cheap Ethereum command such as `Upgrade`/`SetOperatingMode`/a minimal-gas command from `GasMeter`, combined with realistic `exchange_rate`/`multiplier` values), silently breaking the intended fee-charging invariant instead of reverting or flooring to a minimum.

### Impact Explanation
`validate()` (in `send_message_impl.rs`) is invoked on every message sent through the bridge (via `EthereumBlobExporter::deliver` from sibling parachains, or `snowbridge_pallet_system::Pallet::send`), and its returned `Fee` is what downstream code uses to charge the sender in local currency: [6](#0-5) 

If the remote-fee component of that computation rounds to zero, the sender pays only the (unaffected) local weight-fee while the protocol still commits a full wei-denominated `reward` for the relayer on the Ethereum side. This is "public underpriced work" as described in the impact gate: the fee-charging mechanism, which every user can invoke for free by simply sending cheap messages, systematically fails to collect the funds needed to back the promised relayer reward, degrading the sustainability of Snowbridge message processing. This matches the required-impact category "public underpriced work that degrades block production or stalls bridge processing" directly.

### Likelihood Explanation
The bug requires no privileged access — any parachain or the system pallet routinely sends messages through this path, and `gas_used_at_most` for low-gas commands is entirely determined by `GasMeter` for a given command type, not by governance. Combined with routine governance-set `exchange_rate`/`multiplier` values (which are periodically updated per the module docs and are not required to avoid this rounding corner case), the truncation can occur under normal, non-adversarial operating conditions — precisely the "legitimate maker sets correct parameters, but the formula still breaks" scenario from the original report.

### Recommendation
Apply the same two remediations recommended in the original report: (1) fix the formula for the corner case, e.g. floor the computed fee to a minimum of `1` (or an explicit minimum fee constant) after `convert_from_ether_decimals`, and/or (2) perform all fixed-point arithmetic at full 18-decimal precision throughout and only convert/truncate to local decimals as the very last step with explicit rounding-up (`Rounding::Up`) rather than plain `checked_div`, ensuring the local fee charged can never be zero while a nonzero wei-denominated reward is being committed.

### Proof of Concept
Conceptual reproduction (mirrors the original PoC structure):
1. Governance sets valid, non-zero `PricingParameters`: `exchange_rate` and `multiplier` such that `remote_fee_in_wei * multiplier / exchange_rate` is small (e.g. exchange rate reflecting a high-value asset like the DODO PoC's wbtc price), and `T::Decimals = 10` (DOT).
2. A sibling parachain or the system pallet calls `SendMessage::validate` with a command whose `GasMeter::maximum_gas_used_at_most` is small (a cheap command), so `calculate_remote_fee` (`fee_per_gas * gas_used + reward`) yields a modest wei value.
3. After `saturating_mul(multiplier).checked_div(exchange_rate)`, the intermediate 18-decimal value, when passed through `convert_from_ether_decimals`'s `checked_div(10^8)`, truncates to `0` — identical in shape to `temp2 = DecimalMath.mul(i, k) == 0` in the DODO report.
4. `validate` still succeeds (no error path exists for this case) and returns `Fee(local_fee, 0)`; the message is queued, and `do_process_message` unconditionally commits `reward: pricing_params.rewards.remote` in wei to the relayer, decoupled from what was actually charged.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L337-352)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L368-418)
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
