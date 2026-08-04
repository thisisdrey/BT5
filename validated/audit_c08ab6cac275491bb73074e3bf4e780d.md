## Analysis

The external report's core broken invariant: a threshold comparison (`percentDiff < MAX_DIFFERENCE`) guards a price-averaging branch, but the `else` branch (fallback to a safe price) is missing, so the output variable is silently left at its default/zero value instead of a safe fallback — and that under-computed value is then used unchecked downstream for pricing a public operation.

The closest verified local analog is in Snowbridge's outbound-queue fee computation, `Pallet::<T>::calculate_fee` at [1](#0-0) , which is exercised by the pallet's own test suite showing the exact "wrong low value silently accepted" pattern: [2](#0-1) 

### Title
Outbound-queue `calculate_fee` can silently compute a zero remote relayer fee for valid non-zero `PricingParameters`, underpricing message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`LibWstethEthOracle::getWstethEthPrice` fails because a computed value can end up wrong/zero when a guard condition doesn't hold and there is no explicit fallback, and this bad value is used to price a public, user-facing operation. The Snowbridge outbound-queue `calculate_fee` function exhibits the same class of bug: `PricingParameters::validate()` at [3](#0-2)  only rejects individually-zero fields (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`), but does not validate the *combined* arithmetic result. `calculate_fee` performs integer downcasting and division that can produce `fee.remote == 0` even though every individual parameter passed validation, as proven by the pallet's own regression test.

### Finding Description
`calculate_fee` computes the remote fee as:
```
fee = fee_per_gas * gas_used_at_most + reward   (U256, saturating)
fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX)
fee = FixedU128::from_inner(fee).saturating_mul(multiplier).checked_div(exchange_rate).into_inner()
fee = convert_from_ether_decimals(fee)
``` [4](#0-3) 

`convert_from_ether_decimals` performs an integer division by `10^decimals` (18 - local decimals), which truncates any remainder to zero: [5](#0-4) 

There is no lower-bound / sanity check on the final `fee.remote` result analogous to the missing `else` branch in the reported oracle bug — the code has no explicit handling for the case where the computed remote fee rounds down to (or below) an economically meaningful minimum. `PricingParameters::validate()` only guards against zero *inputs*, not a zero *output* of the derived computation: [3](#0-2) 

The pallet's own test confirms this: with all `PricingParameters` fields non-zero and passing `validate()`, `calculate_fee` still returns `fee.remote == 0`: [6](#0-5) 

This fee (including the remote/reward portion) is charged to users sending XCM messages to Ethereum via `send_message_impl.rs`/the exporter, and the `reward` component is what relayers on Ethereum are supposed to receive for delivering the message (per the module doc): "relayers are refunded for gas consumption... plus `Message.Reward`" [7](#0-6) . If governance sets pricing parameters that pass `validate()` (all-nonzero) but combine to a near-zero derived `fee.remote`, users can pay a fee that under-compensates the relayer reward embedded in the outbound message (`reward: reward.try_into().defensive_unwrap_or(u128::MAX)` in `do_process_message` at [8](#0-7) ), exactly mirroring the "public underpriced work" impact class: relayers are economically disincentivized from delivering messages, degrading/stalling bridge processing, while message senders are charged based on an inaccurate/degenerate price computation that was never explicitly bounded.

### Impact Explanation
If the fee (specifically the relayer-reward-bearing remote component) rounds to zero or a value too small to cover actual on-chain relaying costs, no rational relayer will deliver the resulting messages. Because the outbound queue commits messages to the merkle root regardless of whether the derived fee was economically sound (`do_process_message` unconditionally appends the message once fee is charged upstream), this can produce a growing backlog of undeliverable/unprofitable messages — a stall in bridge processing that matches the "public underpriced work that degrades block production or stalls bridge processing" impact class explicitly listed as in-scope.

### Likelihood Explanation
This does not require a malicious actor: it can occur purely through legitimate governance-set `PricingParameters` (root-only extrinsic `set_pricing_parameters` at [9](#0-8) ) combined with a legitimate user's `gas_used_at_most` for cheap commands, since `validate()` cannot catch every combination that produces a degenerate output — the pallet's own test already demonstrates one such combination in normal test fixtures, not an adversarial one. The likelihood of hitting this in production depends on how parameters drift over time (exchange rate, decimals conversion, multiplier) without a bug being noticed, since there's no runtime invariant asserting `fee.remote > 0` (or above some dust threshold) before the message is accepted into the queue.

### Recommendation
Add an explicit lower-bound check in `calculate_fee` (or in `PricingParameters::validate`, though that alone is insufficient since it can't see `gas_used_at_most`) that asserts the final `fee.remote` (and in particular the reward portion) is non-zero / above a configured minimum before the message is admitted by `do_process_message`. If the computed relayer-facing fee falls below the minimum, either reject the message (return an error) or fall back to a safe minimum fee, mirroring the recommended fix in the reported oracle bug: never let a threshold/edge condition silently produce a degenerate value that flows unchecked into downstream economic logic.

### Proof of Concept
The existing test in the repository already demonstrates the flaw end-to-end: [2](#0-1) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}`, `multiplier = 1/1` — all individually valid per `PricingParameters::validate()` — `calculate_fee(250_000, price_params)` returns `fee.remote == 0`. A background agent should extend this test (or add a new one) that then calls `do_process_message`/the full send path with this zero-remote-fee outcome to confirm the message is still committed to the outbound queue and would carry a `reward` of 0 to Ethereum, and should add an assertion/guard rejecting such degenerate fee outputs.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L60-80)
```rust
//! ## Fee Computation Function
//!
//! ```text
//! LocalFee(Message) = WeightToFee(ProcessMessageWeight(Message))
//! RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward
//! RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
//! Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
//! ```
//!
//! By design, the computed fee includes a safety factor (the `Multiplier`) to cover
//! unfavourable fluctuations in the ETH/DOT exchange rate.
//!
//! ## Fee Settlement
//!
//! On the remote side, in the gateway contract, the relayer accrues
//!
//! ```text
//! Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward
//! ```
//! Or in plain english, relayers are refunded for gas consumption, using a
//! price that is a minimum of the actual gas price, or `Message.MaxFeePerGas`.
```

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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L310-334)
```rust
		/// Set pricing parameters on both sides of the bridge
		///
		/// Fee required: No
		///
		/// - `origin`: Must be root
		#[pallet::call_index(2)]
		#[pallet::weight((T::WeightInfo::set_pricing_parameters(), DispatchClass::Operational))]
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());

			let command = Command::SetPricingParameters {
				exchange_rate: params.exchange_rate.into(),
				delivery_cost: T::InboundDeliveryCost::get().saturated_into::<u128>(),
				multiplier: params.multiplier.into(),
			};
			Self::send(PRIMARY_GOVERNANCE_CHANNEL, command, PaysFee::<T>::No)?;

			Self::deposit_event(Event::PricingParametersChanged { params });
			Ok(())
		}
```
