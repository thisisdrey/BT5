### Title
Fee-calculation truncation lets `calculate_fee` return a zero remote fee for non-zero, validated `PricingParameters` — public underpriced bridge work - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Snowbridge outbound-queue pallet computes the fee an XCM message sender must pay to have a message relayed to Ethereum via `Pallet::calculate_fee` and `calculate_remote_fee`/`convert_from_ether_decimals`. Even when `PricingParameters` pass `validate()` (all fields non-zero), integer truncation in the ether-decimal conversion step can silently reduce the "remote" component of the fee to `0`. This is the same class of bug as the external report: a value that governs a critical downstream calculation (there: TWAP price used for `collateralRatio`/liquidation; here: the relayer reward/gas-refund portion of the delivery fee) is allowed to become an unvalidated degenerate value (`0`) and is used anyway, with no revert or floor enforced.

### Finding Description
`PricingParameters::validate()` only rejects params with any field exactly `0`: [1](#0-0) 

However `Pallet::calculate_fee` derives the final on-chain `Balance` fee by taking the raw U256 remote fee, converting to `u128`, applying the multiplier/exchange-rate as a `FixedU128`, and then doing an **integer division** to strip Ether's 18 decimals down to the chain's native decimals (10 or 12): [2](#0-1) 

`convert_from_ether_decimals` divides by `10^(18 - Decimals)` (i.e. `10^8` for a 10-decimal chain), which truncates any raw fee value smaller than that denominator down to `0`: [3](#0-2) 

There is no check anywhere in `calculate_fee` that the resulting `remote` fee (or the intermediate value before truncation) is non-zero, and no revert path analogous to `ErrorInvalidTwapPrice` in the original report. The pallet's own test suite documents this exact failure mode: with fully non-zero, `validate()`-passing parameters (`exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1`), the computed `fee.remote` is `0`: [4](#0-3) 

This zero fee is subsequently charged to the message sender via `send_message_impl`/`validate`, and the same zero `reward` value is embedded directly into the `CommittedMessage` that gets ABI-encoded and delivered to the Ethereum gateway contract, since `reward` in the committed message is taken straight from `pricing_params.rewards.remote` (a raw, unconverted value) while the fee actually charged to the user goes through the lossy conversion: [5](#0-4) 

The mismatch means a user can be charged (or under-charged) an amount that does not correspond to the real relayer incentive/gas-refund promised on Ethereum, and in the degenerate case the local balance fee collected for the remote leg is `0`.

### Impact Explanation
This falls under the "public underpriced work that degrades block production or stalls bridge processing" pivot: if the remote fee component silently rounds to zero for valid-looking pricing parameters (e.g. after governance sets a low `fee_per_gas`/`reward` intended for a low-decimal-precision scenario, or simply through normal parameter updates that land in the truncation range), relayers submitting messages to Ethereum receive no compensation for that gas/relaying cost from the on-chain fee charged to users. Because relaying is a permissionless, incentive-driven public service, an underpriced (here, zero-priced) remote leg removes the economic incentive to relay messages, which can stall the outbound message queue's delivery to Ethereum — a direct bridge-processing-stall outcome explicitly called out in the impact gate.

### Likelihood Explanation
Governance sets `PricingParameters` via extrinsic (`set_pricing_parameters` in the system pallet) and only `validate()`'s non-zero checks gate updates; there is no check against the truncation threshold introduced by `convert_from_ether_decimals`. Any parameter combination where `fee_per_gas * gas_used + reward < 10^(18-Decimals)` (i.e. below roughly `10^8` wei-equivalent for a 10-decimal chain) will trigger this silently, and the pallet's own regression test confirms the trigger conditions are simple, realistic values (`fee_per_gas=1`, `reward=1`). This does not require a malicious relayer, validator, or governance actor — it is a straightforward arithmetic/rounding flaw reachable through normal parameter configuration and message submission.

### Recommendation
Short term: after computing the final `Balance` fee in `calculate_fee`, explicitly check that the remote component is non-zero whenever the pre-conversion value was non-zero, and revert/`Err` the fee calculation (or floor it to a minimum unit) rather than silently returning `0`. Additionally, use `reward.try_into()` consistently through the same conversion path used for the charged fee, so the value embedded in `CommittedMessage.reward` cannot diverge from what was actually collected. Long term, extend `PricingParameters::validate()` (or add a dedicated invariant check invoked at parameter-update time) to ensure that, given `Decimals`, no valid `fee_per_gas`/`reward`/`exchange_rate`/`multiplier` combination can produce a fully-truncated (zero) delivered fee for realistic gas-used ranges.

### Proof of Concept
Using the pallet's own test harness (`bridges/snowbridge/pallets/outbound-queue/src/test.rs`):
```rust
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params);
assert_eq!(fee.remote, 0); // non-zero, validate()-passing params yield a zero remote fee
``` [4](#0-3)

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
