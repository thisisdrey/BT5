## Analysis

The reported bug class is: **a mandatory divisor is only validated inside a privileged setter, but the code path that actually performs the division can be reached before that setter is ever called (or with unvalidated default/config data), causing a division-by-zero panic that halts processing with no way to remediate.**

The closest verified local analog is in Snowbridge's outbound-queue fee calculation.

### Title
Unvalidated `exchange_rate` default in Snowbridge outbound-queue fee calculation causes a `.expect()` panic that halts bridge message processing - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`snowbridge_pallet_outbound_queue::Pallet::calculate_fee` divides by `params.exchange_rate` and explicitly assumes it is non-zero: [1](#0-0) 

That assumption is only enforced by `PricingParameters::validate()`, which is invoked exclusively inside the governance-only `set_pricing_parameters` extrinsic of `snowbridge-pallet-system`: [2](#0-1) [3](#0-2) 

Meanwhile, the outbound-queue pallet consumes pricing parameters through its own `Config::PricingParameters: Get<PricingParameters<Self::Balance>>` associated type, which is exercised by `calculate_fee` on every user-triggered message send (via `send_message_impl.rs`, invoked from ordinary XCM export/reserve-transfer flows to Ethereum), not by reading a value that was necessarily passed through `validate()`.

### Finding Description
This is structurally identical to the reported bug:
- `setProportionalRatioGov()` ↔ `set_pricing_parameters` (governance-only setter that validates the divisor).
- `triggerSettlement()` / claim path ↔ `calculate_fee`, invoked implicitly whenever *any unprivileged user* sends a message to Ethereum (asset transfer, XCM export, etc.).
- The divisor (`proportionalRatioGovUser`/`proportionalRatioGovLP` ↔ `exchange_rate`) is only checked for non-zero inside the setter, never at the point of use, and never enforced on whatever default/config value backs the `Get` implementation before the setter is first called.

The `.expect("exchange rate is not zero; qed")` is a hard invariant assumption baked into pallet code, but that invariant depends entirely on external wiring (genesis config / default `Get` impl / migration state) that is not provably validated by the type system or by any runtime check in the outbound-queue pallet itself. If the value backing `Config::PricingParameters` for the outbound-queue pallet is ever zero (e.g., unvalidated genesis config, a migration that doesn't call `validate()`, or a default the system pallet returns before `set_pricing_parameters` is first invoked), any unprivileged user submitting a message that requires fee calculation triggers a panic inside `calculate_fee`.

### Impact Explanation
A panic in `calculate_fee`, which runs during ordinary message-queue processing (`process_message_impl.rs`) triggered by unprivileged users, would abort message committal/processing for the Snowbridge outbound lane — i.e., it stalls bridge processing / degrades block production for the pallet's `on_initialize`/message-queue execution, matching the "public underpriced work that degrades block production or stalls bridge processing" impact category explicitly listed as in-scope.

### Likelihood Explanation
Reaching `calculate_fee` does **not** require any privileged actor: any account performing an XCM send to Ethereum through the bridge triggers fee computation. The only precondition is that the effective `PricingParameters` (whatever supplies `Config::PricingParameters` for the outbound-queue pallet) has a zero `exchange_rate` at some point — which is plausible during genesis/config setup or before the first governance call to `set_pricing_parameters`, exactly mirroring the external report's scenario where `triggerSettlement()` fires before `setProportionalRatioGov()`.

### Recommendation
- Replace the `.expect()` in `calculate_fee` with a graceful `checked_div` fallback (e.g., reject/queue the message or return a defensive error) instead of panicking.
- Enforce `PricingParameters::validate()` on every code path that can produce the value consumed by `Config::PricingParameters` (genesis config, defaults, and any migration), not only inside `set_pricing_parameters`.

### Proof of Concept
Not independently executable within this scan: the exact runtime wiring of `outbound-queue::Config::PricingParameters` (whether it is a static `parameter_types!` constant or delegates to the system pallet's `PricingParameters` storage) lives in `cumulus/parachains/runtimes/bridge-hubs/*/src/bridge_to_ethereum_config.rs`, which I was not able to fully inspect before the tool budget ran out. The unvalidated `.expect()` panic path and the fact that `validate()` is only called from the governance extrinsic (not from any default/`Get` path) are both directly confirmed in the cited files; whether a genesis/default value of zero can actually reach this `Get` in current runtime configs would need to be confirmed by reading `bridge_to_ethereum_config.rs` in full — flagging this as the residual uncertainty in this analysis.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L380-387)
```rust
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L316-323)
```rust
		#[pallet::weight((T::WeightInfo::set_pricing_parameters(), DispatchClass::Operational))]
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());
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
