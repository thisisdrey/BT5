## Analysis Summary

The strongest local analog to the Balancer oracle-deviation lock-up is in Snowbridge's outbound message fee calculation, which converts an ETH-denominated fee into DOT using a governance-set `exchange_rate`, and blindly `.expect()`s that a `checked_div` failure can only be caused by a zero divisor — while in fact `checked_div` on `FixedU128` also returns `None` on **overflow**. This turns a validated-but-extreme (not literally zero) pricing parameter into a guaranteed transaction/runtime panic reachable by any ordinary user who sends a cross-chain message to Ethereum.

### Title
Panic-inducing false assumption in Snowbridge outbound fee calculation can be triggered by any user, DoSing bridge message submission - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`OutboundQueue::calculate_fee` divides a `FixedU128`-encoded fee value by `PricingParameters.exchange_rate` and unwraps the result with `.expect("exchange rate is not zero; qed")`. This message is only true if `checked_div` can only fail on a zero divisor, but `FixedU128::checked_div` also returns `None` on arithmetic overflow. `PricingParameters::validate()` only rejects an `exchange_rate` of exactly zero, not small-but-nonzero values that make overflow in the fee-scaling arithmetic possible. Since `calculate_fee` runs inside `validate()`, which is invoked on every user-initiated bridge send (e.g. any reserve-asset transfer to Ethereum via the XCM exporter), an unprivileged user's normal transaction can trigger the panic once pricing parameters are in this state.

### Finding Description
`calculate_fee` is defined at [1](#0-0) :
```
let fee = FixedU128::from_inner(fee)
    .saturating_mul(params.multiplier)
    .checked_div(&params.exchange_rate)
    .expect("exchange rate is not zero; qed")
    .into_inner();
```
The only guard on `exchange_rate` is in `PricingParameters::validate()` [2](#0-1) , which rejects `FixedU128::zero()` only — there is no lower bound, and no bound relating `exchange_rate` to `fee_per_gas`/`rewards.remote`/`multiplier` to keep the division result within `u128`. This validation is invoked from `set_pricing_parameters` [3](#0-2) , which only enforces the same "not-zero" invariants.

`calculate_fee` is called directly from the public message-validation path used by every bridge sender: [4](#0-3) , which itself is invoked from the XCM exporter used by ordinary XCM transfers to Ethereum [5](#0-4) .

Because `FixedU128::checked_div` returns `None` both for a zero divisor and for internal overflow of the scaled multiplication (see the generic fixed-point `checked_div` implementation) [6](#0-5) , any combination of `fee_per_gas`, `rewards.remote`, `multiplier`, and a small-but-nonzero `exchange_rate` that pushes the intermediate `u128` fixed-point value past its bound will cause `checked_div` to legitimately return `None` for a non-zero-divisor reason, and the `.expect()` will panic. There is no `checked_*`/`saturating_*` fallback here, unlike elsewhere in the same function (`saturating_mul`, `defensive_unwrap_or`).

### Impact Explanation
A panic occurring inside dispatch of an ordinary, permissionless XCM transfer/message-send extrinsic is not a benign transaction failure — panics unwound inside runtime dispatch surface as host-function/WASM traps during block execution, which can prevent the block from being produced or imported cleanly, i.e. it is exactly the class of "implementation bug that can bring down a Substrate-based chain" and "public underpriced work that ... stalls bridge processing" called out in the impact gate. Once pricing parameters land in the vulnerable (but currently "valid") range, *every* subsequent bridge-send transaction from *any* account hits the same panic, permanently stalling the Ethereum outbound message pipeline until parameters are fixed by governance — funds already deposited/reserved for a pending transfer are effectively stuck, mirroring the "settlement reverts, funds locked" pattern of the source report.

### Likelihood Explanation
`exchange_rate`, `fee_per_gas`, `multiplier`, and `rewards.remote` are periodically updated by governance via `set_pricing_parameters` (the pallet doc itself notes they are manually adjusted "every few weeks") [7](#0-6) , and the only safety check performed is non-zero, not overflow-safety. A parameter update that is entirely reasonable in intent (e.g., a large fee-per-gas bump during Ethereum gas spikes, combined with a depressed ETH/DOT exchange rate during volatility) can land in the unsafe range without the operator realizing it, because `validate()` gives no signal. From that point on, the trigger condition (any user submitting a bridge message) is guaranteed and requires no attacker sophistication.

### Recommendation
- Replace `.expect("exchange rate is not zero; qed")` with a `checked_div(...).ok_or(...)`-based fallible path that surfaces a proper `SendError`/`DispatchError` instead of panicking, mirroring the existing `defensive_unwrap_or` fallback pattern already used earlier in the same function.
- Strengthen `PricingParameters::validate()` to reject parameter combinations that could overflow the fee-scaling arithmetic (e.g., bound `exchange_rate` below by a sane minimum, or perform a trial computation with worst-case gas/reward values before accepting new parameters).
- Add a regression/fuzz test that exercises `calculate_fee` with extreme-but-nonzero `exchange_rate` values to ensure it degrades gracefully instead of panicking.

### Proof of Concept
1. Governance calls `set_pricing_parameters` with `exchange_rate` set to the smallest positive `FixedU128` value (nonzero, so it passes `validate()`), and a realistic-to-high `fee_per_gas`/`rewards.remote`/`multiplier`.
2. Any user (e.g., via a normal reserve-asset transfer to Ethereum through the XCM `EthereumBlobExporter`) causes `OutboundQueue::validate()` to run, which calls `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())`.
3. Inside `calculate_fee`, `FixedU128::from_inner(fee).saturating_mul(params.multiplier)` produces a large inner value; `.checked_div(&params.exchange_rate)` overflows internally and returns `None`.
4. `.expect("exchange rate is not zero; qed")` panics even though `exchange_rate` is nonzero, aborting the message-send path for that transaction and, more importantly, for every subsequent bridge-send transaction until the parameters are corrected — a persistent, unprivileged-user-triggerable denial of the outbound bridge queue.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-58)
```rust
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
//!
//! This is an interim measure. Once ETH/DOT liquidity pools are available in the Polkadot network,
//! we'll use them as a source of pricing info, subject to certain safeguards.
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L315-323)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight((T::WeightInfo::set_pricing_parameters(), DispatchClass::Operational))]
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-60)
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
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L117-132)
```rust
		let mut converter =
			XcmConverter::<ConvertAssetId, ()>::new(&message, expected_network, agent_id);
		let (command, message_id) = converter.convert().map_err(|err|{
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "unroutable due to pattern matching.");
			SendError::Unroutable
		})?;

		let channel_id: ChannelId = ParaId::from(para_id).into();

		let outbound_message = Message { id: Some(message_id.into()), channel_id, command };

		// validate the message
		let (ticket, fee) = OutboundQueue::validate(&outbound_message).map_err(|err| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;
```

**File:** substrate/primitives/arithmetic/src/fixed_point.rs (L882-905)
```rust
		impl CheckedDiv for $name {
			fn checked_div(&self, other: &Self) -> Option<Self> {
				if other.0 == 0 {
					return None;
				}

				let lhs: I129 = self.0.into();
				let rhs: I129 = other.0.into();
				let negative = lhs.negative != rhs.negative;

				// Note that this uses the old (well-tested) code with sign-ignorant rounding. This
				// is equivalent to the `SignedRounding::NearestPrefMinor`. This means it is
				// expected to give exactly the same result as `const_checked_div` when the result
				// is positive and a result up to one epsilon greater when it is negative.
				multiply_by_rational_with_rounding(
					lhs.value,
					Self::DIV as u128,
					rhs.value,
					Rounding::from_signed(SignedRounding::Minor, negative),
				)
				.and_then(|value| from_i129(I129 { value, negative }))
				.map(Self)
			}
		}
```
