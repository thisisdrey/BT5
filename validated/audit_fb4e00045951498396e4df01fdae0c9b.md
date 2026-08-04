## Title
`AssetFeeAsExistentialDepositMultiplier` rounds XCM weight fees **down** when converting to non-native assets, letting execution be bought below cost - ([File: cumulus/parachains/common/src/xcm_config.rs])

### Summary
`TakeFirstAssetTrader::buy_weight`/`quote_weight` (`cumulus/primitives/utility/src/lib.rs`) determine how much of a non-native fungible asset must be taken from the incoming XCM `payment` to pay for `weight`, by calling `FeeCharger::charge_weight_in_fungibles`. For assets priced via `pallet-asset-rate`, this delegates to `AssetFeeAsExistentialDepositMultiplier::charge_weight_in_fungibles` (`cumulus/parachains/common/src/xcm_config.rs:54-72`), which calls `BalanceConverter::to_asset_balance(amount, asset_id)`. That conversion is implemented in `pallet-asset-rate`'s `ConversionToAssetBalance::to_asset_balance` (`substrate/frame/asset-rate/src/lib.rs:268-280`), which computes `(1/rate).saturating_mul_int(balance)`. `FixedU128::saturating_mul_int` truncates (rounds toward zero), so the resulting `asset_amount` is systematically rounded **down** rather than up, in a code path whose entire purpose is to compute the minimum charge required to cover the weight actually consumed.

### Finding Description
The GLP analogy is a hardcoded rounding direction on a security-relevant price lookup that under-protects the protocol: `glpManager.getPrice(false)` (minimize) is used where `getPrice(true)` (maximize) is required to compute a floor value (`_minGlp`), so the check `mintAmount >= _minGlp` becomes too lenient and users can receive less than fair value without a revert.

The analogous local pattern is in `cumulus/parachains/common/src/xcm_config.rs`:

```rust
fn charge_weight_in_fungibles(asset_id, weight) -> Result<Balance, XcmError> {
    let amount = WeightToFee::weight_to_fee(&weight);
    let asset_amount = BalanceConverter::to_asset_balance(amount, asset_id)
        .map_err(|_| XcmError::TooExpensive)?;
    Ok(asset_amount)
}
``` [1](#0-0) 

and in `pallet-asset-rate`:

```rust
fn to_asset_balance(balance, asset_kind) -> Result<Balance, Error<T>> {
    let rate = ConversionRateToNative::<T>::get(asset_kind)...;
    Ok(FixedU128::from_u32(1)
        .checked_div(&rate)...
        .saturating_mul_int(balance))
}
``` [2](#0-1) 

`saturating_mul_int` performs integer division that rounds toward zero (down), unlike other amount-in/amount-out computations in the codebase that deliberately round in the safe direction, e.g. `get_amount_in` in `pallet-asset-conversion` explicitly rounds **up** with `.checked_add(&One::one())` to avoid under-collection:

```rust
let result = numerator.checked_div(&denominator)...checked_add(&One::one())...;
``` [3](#0-2) 

That contrast shows the codebase is aware that fee/amount computations charged *from* a user to cover a cost must round up, yet the asset-rate → XCM weight-charging path does the opposite: it rounds the *required* payment down.

This `required_amount` is exactly what `TakeFirstAssetTrader::buy_weight` withdraws from the XCM `payment` holding to cover `weight`:

```rust
let required_amount: u128 =
    match FeeCharger::charge_weight_in_fungibles(fungibles_asset_id.clone(), weight)
        .map(|amount| amount.max(Fungibles::minimum_balance(fungibles_asset_id.clone())))
    { Ok(a) => a, Err(_) => return Err((payment, XcmError::Overflow)) };
let required = used.id.into_asset(required_amount.into());
let Some(imbalance) = payment.try_take(required.into())... 
``` [4](#0-3) 

There is no compensating "round up" step anywhere in `charge_weight_in_fungibles`, `buy_weight`, or `quote_weight` — the only floor applied is `.max(minimum_balance)`, which does not correct the truncation of the fee itself, only the ED-floor case.

### Impact Explanation
Every XCM message that pays execution weight in a non-native (asset-rate-priced) fungible asset via `TakeFirstAssetTrader`/`AssetFeeAsExistentialDepositMultiplier` is charged strictly less than the actual native-equivalent cost of the weight it consumes, because the division that converts native fee → asset units always truncates toward zero. This is a systemic underpricing of execution across every such XCM `BuyExecution` instruction, not an occasional edge case — matching the impact-gate category of "public underpriced work that degrades block production." An attacker can send a stream of XCM messages priced in a low-rate asset, each slightly underpaying, to consume parachain execution weight for less than intended cost, at scale enabling weight-based spam/DoS at below-market pricing.

### Likelihood Explanation
High: this is not a rare boundary condition — truncation occurs on essentially *every* non-exact-multiple `weight_to_fee(weight)/rate` computation, since asset rates and weight fees are arbitrary values unlikely to divide evenly. No privileged actor, malicious peer, or governance action is needed; any account submitting XCM `BuyExecution` with a non-native asset pays less than accounted for automatically, every time rounding occurs in its favor.

### Recommendation
Round the asset amount required to cover weight **up**, not down, mirroring the `get_amount_in`-style "round toward the protocol’s favor" convention used elsewhere in the SDK:
```diff
 fn to_asset_balance(balance, asset_kind) -> Result<Balance, Error<T>> {
     let rate = ...;
-    Ok(FixedU128::from_u32(1)
-        .checked_div(&rate)...
-        .saturating_mul_int(balance))
+    // round up to avoid undercharging for the requested native balance
+    Ok(FixedU128::from_u32(1)
+        .checked_div(&rate)...
+        .checked_rounding_mul_int(balance, Rounding::Up)...)
 }
```
Alternatively, apply a ceiling adjustment specifically in `charge_weight_in_fungibles` before returning `asset_amount`, since this call site is the one with the security requirement (charging *at least* the cost of weight consumed), while `from_asset_balance` (asset→native) may legitimately want to round the other way for user-favorable refunds.

### Proof of Concept
1. Configure a parachain XCM config using `AssetFeeAsExistentialDepositMultiplier<Runtime, WeightToFee, AssetRate, Instance>` as the `FeeCharger` for `TakeFirstAssetTrader`, with an asset whose `ConversionRateToNative` rate is set to a value that does not evenly divide the computed native fee (e.g., rate = `FixedU128::from_rational(3, 1)`, i.e. `1 native = 3 asset units` conceptually, so `1/rate = 0.333...`).
2. Compute `amount = WeightToFee::weight_to_fee(weight)` for some `weight`, e.g. `amount = 100` native units.
3. Call `AssetRate::to_asset_balance(100, asset_id)`: `1/3 * 100 = 33.33...`, and `saturating_mul_int` truncates to `33`, i.e. `Ok(33)`. The true amount required to fully cover 100 native units of fee, given the 1:3 rate, should round up to `34`.
4. Submit an XCM message with `BuyExecution { fees: (asset_id, 33), weight_limit }`. `TakeFirstAssetTrader::buy_weight` computes `required_amount = 33` (since `charge_weight_in_fungibles` returns 33), takes exactly `33` units from `payment`, and allows the message to execute the full `weight` — one asset unit's worth of native fee (worth `0.33` native by the truncated conversion) is never collected.
5. Repeating this at scale (many messages, each systematically underpaying by the rounding remainder) allows an attacker to consume more aggregate parachain weight per unit of value paid than intended, degrading fair fee collection and potentially enabling weight-based spam.

Note: I was unable to fully trace every runtime configuration that wires `AssetFeeAsExistentialDepositMultiplier` with `pallet-asset-rate` in production (some runtimes may use `UnityAssetBalanceConversion` for native-only fee assets, which is unaffected since it is 1:1). Confirming exact production impact per-runtime would need enumerating each `Config` for `ChargeWeightInFungibles`/`BalanceConverter` in the various parachain runtimes, which the index does not fully expose — a Devin session with full repo access could grep all `type FeeCharger = AssetFeeAsExistentialDepositMultiplier<...>` bindings to confirm which live runtimes are exposed to this rounding direction.

### Citations

**File:** cumulus/parachains/common/src/xcm_config.rs (L54-72)
```rust
	fn charge_weight_in_fungibles(
		asset_id: <pallet_assets::Pallet<Runtime, AssetInstance> as Inspect<
			AccountIdOf<Runtime>,
		>>::AssetId,
		weight: Weight,
	) -> Result<
		<pallet_assets::Pallet<Runtime, AssetInstance> as Inspect<AccountIdOf<Runtime>>>::Balance,
		XcmError,
	> {
		let amount = WeightToFee::weight_to_fee(&weight);
		// If the amount gotten is not at least the ED, then make it be the ED of the asset
		// This is to avoid burning assets and decreasing the supply
		let asset_amount = BalanceConverter::to_asset_balance(amount, asset_id)
			.map_err(|error| {
				tracing::debug!(target: "xcm::charge_weight_in_fungibles", ?error, "AssetFeeAsExistentialDepositMultiplier cannot convert to valid balance (possibly below ED)");
				XcmError::TooExpensive
			})?;
		Ok(asset_amount)
	}
```

**File:** substrate/frame/asset-rate/src/lib.rs (L268-280)
```rust
	fn to_asset_balance(
		balance: BalanceOf<T>,
		asset_kind: AssetKindOf<T>,
	) -> Result<BalanceOf<T>, pallet::Error<T>> {
		let rate = pallet::ConversionRateToNative::<T>::get(asset_kind)
			.ok_or(pallet::Error::<T>::UnknownAssetKind.into())?;

		// We cannot use `saturating_div` here so we use `checked_div`.
		Ok(FixedU128::from_u32(1)
			.checked_div(&rate)
			.ok_or(pallet::Error::<T>::Overflow.into())?
			.saturating_mul_int(balance))
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1456-1460)
```rust
			let result = numerator
				.checked_div(&denominator)
				.ok_or(Error::<T>::Overflow)?
				.checked_add(&One::one())
				.ok_or(Error::<T>::Overflow)?;
```

**File:** cumulus/primitives/utility/src/lib.rs (L195-218)
```rust
		// Calculate how much we should charge in the asset_id for such amount of weight
		// Require at least a payment of minimum_balance
		// Necessary for fully collateral-backed assets
		let required_amount: u128 =
			match FeeCharger::charge_weight_in_fungibles(fungibles_asset_id.clone(), weight)
				.map(|amount| amount.max(Fungibles::minimum_balance(fungibles_asset_id.clone())))
			{
				Ok(a) => a,
				Err(_) => return Err((payment, XcmError::Overflow)),
			};

		// Convert to the same kind of asset, with the required fungible balance
		let required = used.id.into_asset(required_amount.into());

		// Subtract required from payment.
		// Note: `payment` may contain multiple assets, but we only take from the first fungible
		// asset that was matched above. Any remaining assets stay in `payment` and are returned.
		let Some(imbalance) = payment
			.try_take(required.into())
			.ok()
			.and_then(|taken| taken.fungible.into_iter().next().map(|(_, v)| v))
		else {
			return Err((payment, XcmError::TooExpensive));
		};
```
