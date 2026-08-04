Based on the evidence gathered, this is a strong local analog to the reported bug.

### Title
`SignedGas::from_adjusted_deposit_charge()` rounds a positive storage-deposit charge towards zero instead of away from it, systematically undercharging Ethereum-gas for `pallet-revive` contract execution - (File: `substrate/frame/revive/src/metering/gas.rs`)

### Summary
`SignedGas::from_adjusted_deposit_charge` converts a `StorageDeposit::Charge(amount)` (a positive amount owed by the caller for storage growth) into a positive gas amount using `multiplier.saturating_mul_int(*amount)` [1](#0-0) . `saturating_mul_int` internally uses `SignedRounding::Minor` ("towards zero") for the multiplication [2](#0-1) . For a positive charge this always rounds down, exactly mirroring the Notional `convertFromStorage()` bug where a debt-representing negative value was divided with rounding towards zero instead of away from the caller's benefit.

### Finding Description
In Ethereum-execution mode, `EthTxInfo::gas_consumption` combines the fixed extrinsic fee with the consumed storage deposit and converts the sum to a gas amount via `SignedGas::from_adjusted_deposit_charge`: [3](#0-2) 

Internally, `from_adjusted_deposit_charge` treats `Charge` as `Positive(multiplier.saturating_mul_int(*amount))` [1](#0-0) . `saturating_mul_int`/`checked_mul_int` use `Rounding::from_signed(SignedRounding::Minor, negative)`, which for `negative == false` maps to `Down` (comment: "Note that this uses ... sign-ignorant rounding ... equivalent to `SignedRounding::NearestPrefMinor`") [2](#0-1) [4](#0-3) .

This gas total feeds directly into `weight_remaining` and `gas_left`, which govern how much execution weight a transaction is still allowed to consume relative to its declared `eth_gas_limit` [5](#0-4) [6](#0-5) . Because the deposit-to-gas conversion always rounds the charge down (never up), every accounted "cost" of storage growth is systematically underestimated by up to one unit of gas granularity. This is the mirror image of the correct direction: a charge against the caller should round up (favoring the protocol/gas budget), not down (favoring the caller). The refund branch (`Negative`) is likewise magnitude-rounded down via the same `Minor` rule, which does favor the protocol — confirming the charge branch is inconsistent and wrong relative to the refund branch's own (correct) direction.

Existing guards do not stop this: `saturating_mul_int` only saturates on overflow, it does not correct rounding direction; there is no compensating "round up" step anywhere in the `gas.rs`/`math.rs` conversion path for `Positive`/`Charge`.

### Impact Explanation
Every EVM-mode transaction that grows contract storage gets its storage-deposit cost translated into gas using floor rounding. This lets a caller who repeatedly triggers many small storage-growing operations consume slightly more real weight/deposit than their gas budget should allow (the gas-accounting side undercounts the true cost), which is a public, underpriced-work pattern that can be repeated at scale by any contract caller to extract more execution/storage capacity per unit of paid gas than intended, degrading the gas-to-resource pricing guarantee that the meter is supposed to enforce chain-wide.

### Likelihood Explanation
This path executes on every single call in `TransactionLimits::EthereumGas` mode that consumes any storage deposit charge, and requires no special privileges — any user submitting an Ethereum-style transaction to a `pallet-revive` deployment (e.g., Asset Hub Westend, pallet index 60) exercises it. The systematic (not occasional) nature of the rounding (it happens on essentially every non-zero charge) makes the aggregate underpricing effect straightforward to accumulate with repeated small storage writes.

### Recommendation
In `SignedGas::from_adjusted_deposit_charge`, round the `Charge` branch away from zero (up) instead of using the default `saturating_mul_int` (which rounds towards zero). Use `checked_mul_int`-equivalent logic with `SignedRounding::Major` (or add 1 unit / use `multiply_by_rational_with_rounding(..., Rounding::Up)` directly on the underlying value) for the `StorageDeposit::Charge` case, while keeping the `Refund` branch rounding down (in the protocol's favor) as it currently does:
```rust
pub fn from_adjusted_deposit_charge(deposit: &StorageDeposit<BalanceOf<T>>) -> Self {
    let multiplier = T::FeeInfo::next_fee_multiplier_reciprocal();
    match deposit {
        StorageDeposit::Charge(amount) => {
            // round the charge up (away from zero) so the caller is never undercharged
            Positive(multiplier.saturating_mul_int_with_rounding(*amount, SignedRounding::Major))
        },
        StorageDeposit::Refund(amount) => {
            Self::safe_new_negative(multiplier.saturating_mul_int(*amount))
        },
    }
}
```
(This requires exposing a rounding-parameterized variant of `saturating_mul_int`/`checked_mul_int`, analogous to `const_checked_mul_with_rounding`.)

### Proof of Concept
Given `T::FeeInfo::next_fee_multiplier_reciprocal()` returning a `FixedU128` whose inner value causes `amount * multiplier` to have a nonzero fractional remainder (e.g., multiplier `= 1/3`, `amount = 10` deposit units), `saturating_mul_int` computes `10/3 = 3` (rounded down) instead of `4` (rounded up), understating the gas-equivalent charge by 1 unit per such operation. Repeating a storage-growing call `N` times inflates the aggregate discrepancy to `~N` gas units, letting the caller under-report consumed gas relative to actual chain resource usage — directly analogous to the `-14 / 4 == -3` (rounds toward zero) example from the Notional PoC, applied here to the positive `Charge` conversion instead of a negative debt conversion.

**Uncertainty note:** I was not able to directly execute this code or trace the exact production runtime configuration (e.g., which live Asset Hub runtime enables `TransactionLimits::EthereumGas` mode with what `FeeInfo` multiplier values) to quantify the real-world dust magnitude; this assessment is based on static analysis of the rounding-direction logic in `gas.rs`/`fixed_point.rs`/`per_things.rs` only.

### Citations

**File:** substrate/frame/revive/src/metering/gas.rs (L70-79)
```rust
	pub fn from_adjusted_deposit_charge(deposit: &StorageDeposit<BalanceOf<T>>) -> Self {
		let multiplier = T::FeeInfo::next_fee_multiplier_reciprocal();

		match deposit {
			StorageDeposit::Charge(amount) => Positive(multiplier.saturating_mul_int(*amount)),
			StorageDeposit::Refund(amount) => {
				Self::safe_new_negative(multiplier.saturating_mul_int(*amount))
			},
		}
	}
```

**File:** substrate/primitives/arithmetic/src/fixed_point.rs (L205-217)
```rust
	fn checked_mul_int<N: FixedPointOperand>(self, n: N) -> Option<N> {
		let lhs: I129 = self.into_inner().into();
		let rhs: I129 = n.into();
		let negative = lhs.negative != rhs.negative;

		multiply_by_rational_with_rounding(
			lhs.value,
			rhs.value,
			Self::DIV.unique_saturated_into(),
			Rounding::from_signed(SignedRounding::Minor, negative),
		)
		.and_then(|value| from_i129(I129 { value, negative }))
	}
```

**File:** substrate/frame/revive/src/metering/mod.rs (L358-373)
```rust
	/// Get remaining ethereum gas equivalent.
	///
	/// Converts remaining resources to ethereum gas units:
	/// - For ethereum mode: computes directly from gas accounting
	/// - For substrate mode: converts weight+deposit to gas equivalent
	/// Returns None if resources are exhausted or conversion fails.
	pub fn eth_gas_left(&self) -> Option<BalanceOf<T>> {
		let gas_left = match &self.transaction_limits {
			TransactionLimits::EthereumGas { eth_tx_info, .. } => {
				math::ethereum_execution::gas_left(self, eth_tx_info)
			},
			TransactionLimits::WeightAndDeposit { .. } => math::substrate_execution::gas_left(self),
		}?;

		gas_left.to_ethereum_gas()
	}
```

**File:** substrate/frame/revive/src/metering/mod.rs (L707-723)
```rust
	/// Calculate total gas consumed by weight and storage operations.
	pub fn gas_consumption(
		&self,
		consumed_weight: &Weight,
		consumed_deposit: &DepositOf<T>,
	) -> SignedGas<T> {
		let fixed_fee = T::FeeInfo::fixed_fee(self.encoded_len);
		let deposit_and_fixed_fee =
			consumed_deposit.saturating_add(&DepositOf::<T>::Charge(fixed_fee));
		let deposit_gas = SignedGas::from_adjusted_deposit_charge(&deposit_and_fixed_fee);

		let weight_gas = SignedGas::from_weight_fee(T::FeeInfo::weight_to_fee(
			&consumed_weight.saturating_add(self.extra_weight),
		));

		deposit_gas.saturating_add(&weight_gas)
	}
```

**File:** substrate/frame/revive/src/metering/mod.rs (L725-744)
```rust
	/// Calculate maximal possible remaining weight that can be consumed given a particular gas
	/// limit.
	///
	/// Returns None if remaining gas would not allow any more weight consumption.
	pub fn weight_remaining(
		&self,
		max_total_gas: &SignedGas<T>,
		total_weight_consumption: &Weight,
		total_deposit_consumption: &DepositOf<T>,
	) -> Option<Weight> {
		let fixed_fee = T::FeeInfo::fixed_fee(self.encoded_len);
		let deposit_and_fixed_fee =
			total_deposit_consumption.saturating_add(&DepositOf::<T>::Charge(fixed_fee));
		let deposit_gas = SignedGas::from_adjusted_deposit_charge(&deposit_and_fixed_fee);

		let consumable_fee = max_total_gas.saturating_sub(&deposit_gas).to_weight_fee()?;

		T::FeeInfo::fee_to_weight(consumable_fee)
			.checked_sub(&total_weight_consumption.saturating_add(self.extra_weight))
	}
```

**File:** substrate/primitives/arithmetic/src/per_things.rs (L497-512)
```rust
impl Rounding {
	/// Returns the value for `Rounding` which would give the same result ignorant of the sign.
	pub const fn from_signed(rounding: SignedRounding, negative: bool) -> Self {
		use Rounding::*;
		use SignedRounding::*;
		match (rounding, negative) {
			(Low, true) | (Major, _) | (High, false) => Up,
			(High, true) | (Minor, _) | (Low, false) => Down,
			(NearestPrefMajor, _) | (NearestPrefHigh, false) | (NearestPrefLow, true) => {
				NearestPrefUp
			},
			(NearestPrefMinor, _) | (NearestPrefLow, false) | (NearestPrefHigh, true) => {
				NearestPrefDown
			},
		}
	}
```
