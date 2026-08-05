## Analysis

The external report's core broken invariant: **a component trusts an AMM's instantaneous (spot) reserve ratio for a financial calculation, with no TWAP/staleness/price-impact guard, letting an attacker who temporarily shifts the pool's reserves obtain a favorable price**.

The direct local analog is `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`, which prices non-native transaction fees purely off the live spot reserves of `pallet-asset-conversion` (the in-repo Uniswap-V2-style AMM), with no TWAP and no user-supplied slippage bound.

### Key evidence

`SwapAssetAdapter::withdraw_fee` quotes the fee entirely from current reserves via `QuotePrice::quote_price_tokens_for_exact_tokens`, then immediately executes `SwapCredit::swap_tokens_for_exact_tokens` against the same (unprotected) reserves: [1](#0-0) 

That quote/swap pipeline bottoms out in `pallet_asset_conversion::Pallet::get_amount_in`, computed straight off `get_reserves` (the live pool token balances) — a pure constant-product spot formula with no oracle averaging: [2](#0-1) [3](#0-2) 

The pallet's own trait docs acknowledge the price is only trustworthy absent intervening swaps — i.e., it is explicitly a spot price with no TWAP protection built in: [4](#0-3) 

This `SwapAssetAdapter` is wired into asset-hub-westend's tx-payment config, so it prices *every* fee-in-asset extrinsic against a specific pool's live reserves: [5](#0-4) 

### Why existing guards don't stop it

- `ChargeAssetTxPayment`'s fee-asset conversion path exposes **no user-supplied slippage/maximum parameter** — unlike the general-purpose `swap_tokens_for_exact_tokens` extrinsic which takes `amount_in_max`, the fee-payment adapter always accepts whatever the current pool ratio dictates.
- The only "atomicity" the code guarantees is that quote and swap happen back-to-back *within this one call* — it does nothing to prevent the attacker from moving the pool's reserves in an **earlier extrinsic in the same block** (an ordinary, permissionless `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` call), then having their fee-paying extrinsic execute against the now-skewed ratio, and finally a third extrinsic to reverse the skew and recapture most of the capital — the same self-contained "flash-loan-style" temporary-capital pattern as the Predy report, adapted to Substrate's serial-extrinsic block model instead of a single-transaction flash loan.
- Because the fee side (`A::get()`, e.g. native) is fixed by `pallet-transaction-payment`'s deterministic fee calculation, the attacker's manipulation converts directly into "pay fewer non-native tokens than the pool's true/external price would imply," extracting value from the AMM's liquidity providers/pool state rather than fixing on any oracle-verified rate.

This satisfies the "Polkadot SDK Pivot" concern that public-facing conversions must not rely on unguarded spot state, and is reachable purely by unprivileged accounts submitting ordinary extrinsics in a chosen order within one block — no relayer, validator, collator, or governance actor required.

### Title
Transaction-fee asset conversion in `SwapAssetAdapter` prices off manipulable AMM spot reserves with no TWAP or slippage bound - (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee`/`can_withdraw_fee` convert a user's chosen fee-asset into the native fee using `pallet-asset-conversion`'s instantaneous constant-product reserves, with no TWAP, staleness check, or attacker-facing price-impact cap, and no way for the runtime to bound how far the ratio may have been pushed just before the extrinsic executes.

### Finding Description
`get_amount_in`/`get_amount_out` and `get_reserves` read only the pool account's current token balances at execution time. `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens` then immediately `swap_tokens_for_exact_tokens` on that same live state. There is no mechanism analogous to Uniswap's TWAP oracle, and unlike the explicit `swap_*` extrinsics (which accept `amount_out_min`/`amount_in_max` from the caller), the fee-payment path has no attacker-visible or user-configurable bound at all — whatever the pool's reserves are when the extrinsic executes is accepted unconditionally.

### Impact Explanation
An attacker who also controls (or is) a large liquidity/swap participant in the specific fee-paying pool can shift the reserve ratio in one extrinsic, execute a fee-paying transaction that is under-priced relative to the pool's "fair"/pre-manipulation ratio in a following extrinsic within the same block, and then reverse the shift, extracting value from the pool's liquidity providers each time a fee-in-asset transaction is settled at a manipulated rate. Repeated at scale, this degrades the LP pool's asset backing (loss of funds to LPs) each time such a transaction clears, which maps to the "theft or unbacked mint/unlock" and "runtime bugs that compromise intended behavior" impact classes.

### Likelihood Explanation
Requires no privileged role, relayer, validator, or leaked key — only ordinary, permissionless calls (`swap_exact_tokens_for_tokens`, `add_liquidity`/`remove_liquidity`) combined with a fee-in-asset transaction, orderable by the attacker themselves within a single block they author transactions for. The attack is bounded only by the attacker's own capital and the fee-swap sizes needed to move a given pool's ratio meaningfully, which is realistic for lower-liquidity, non-DOT pools that are nonetheless valid `AssetId`s for `ChargeAssetTxPayment`.

### Recommendation
Add a caller-supplied maximum fee-asset amount (slippage bound) to `ChargeAssetTxPayment`/`OnChargeAssetTransaction::withdraw_fee`, and/or require the fee conversion to use a time-weighted or otherwise smoothed price rather than the single-block spot reserve ratio, rejecting the transaction if the realized conversion price deviates beyond a configured tolerance from a longer-window reference price.

### Proof of Concept
1. Attacker holds asset `X` and native asset `N`, with a live `X`/`N` pool in `pallet-asset-conversion`.
2. Extrinsic 1 (attacker): `swap_exact_tokens_for_tokens([X, N], large_amount, ...)` — sharply skews the `X`/`N` reserve ratio so `X` becomes "cheap" relative to `N` in AMM terms.
3. Extrinsic 2 (attacker, same block): submits any call with `ChargeAssetTxPayment { asset_id: Some(X), .. }`; `SwapAssetAdapter::withdraw_fee` quotes and swaps `X → N` at the now-skewed ratio, so the attacker pays far less `X` than the pre-manipulation market rate for the fixed native fee.
4. Extrinsic 3 (attacker, same block): reverse-swap `N → X` to restore the ratio and recapture most of the capital deployed in step 2, keeping the extracted price difference as profit taken from the pool's liquidity. [1](#0-0) [2](#0-1)

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-176)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;

		// Withdraw the `asset_id` credit for the swap.
		let asset_fee_credit = F::withdraw(
			asset_id.clone(),
			who,
			asset_fee,
			Precision::Exact,
			Preservation::Preserve,
			Fortitude::Polite,
		)
		.map_err(|_| InvalidTransaction::Payment)?;

		let (fee_credit, change) = match S::swap_tokens_for_exact_tokens(
			vec![asset_id, A::get()],
			asset_fee_credit,
			fee,
		) {
			Ok((fee_credit, change)) => (fee_credit, change),
			Err((credit_in, _)) => {
				defensive!("Fee swap should pass for the quoted amount");
				let _ = F::resolve(who, credit_in).defensive_proof("Should resolve the credit");
				return Err(InvalidTransaction::Payment.into());
			},
		};

		// Since the exact price for `fee` has been quoted, the change should be zero.
		ensure!(change.peek().is_zero(), InvalidTransaction::Payment);

		Ok((fee_credit, asset_fee))
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1421-1463)
```rust
		/// Calculates amount in for a given swap `fee`.
		///
		/// Given an output amount of an asset and pair reserves, returns a required input amount
		/// of the other asset.
		pub fn get_amount_in(
			fee: Permill,
			amount_out: &T::Balance,
			reserve_in: &T::Balance,
			reserve_out: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			let amount_out = T::HigherPrecisionBalance::from(*amount_out);
			let reserve_in = T::HigherPrecisionBalance::from(*reserve_in);
			let reserve_out = T::HigherPrecisionBalance::from(*reserve_out);

			if reserve_in.is_zero() || reserve_out.is_zero() {
				Err(Error::<T>::ZeroLiquidity)?
			}

			if amount_out >= reserve_out {
				Err(Error::<T>::AmountOutTooHigh)?
			}

			let fee_complement = fee.left_from_one().deconstruct();
			let numerator = reserve_in
				.checked_mul(&amount_out)
				.ok_or(Error::<T>::Overflow)?
				.checked_mul(&T::HigherPrecisionBalance::from(Permill::ACCURACY))
				.ok_or(Error::<T>::Overflow)?;

			let denominator = reserve_out
				.checked_sub(&amount_out)
				.ok_or(Error::<T>::Overflow)?
				.checked_mul(&T::HigherPrecisionBalance::from(fee_complement))
				.ok_or(Error::<T>::Overflow)?;

			let result = numerator
				.checked_div(&denominator)
				.ok_or(Error::<T>::Overflow)?
				.checked_add(&One::one())
				.ok_or(Error::<T>::Overflow)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1514)
```rust
		pub fn get_reserves(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
		) -> Result<(T::Balance, T::Balance), Error<T>> {
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			let balance1 = Self::get_balance(&pool_account, asset1);
			let balance2 = Self::get_balance(&pool_account, asset2);

			if balance1.is_zero() || balance2.is_zero() {
				Err(Error::<T>::PoolEmpty)?;
			}

			Ok((balance1, balance2))
		}
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-134)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
	/// Measurement units of the asset classes for pricing.
	type Balance: Balance;
	/// Type representing the kind of assets for which the price is being quoted.
	type AssetKind;
	/// Quotes the amount of `asset1` required to obtain the exact `amount` of `asset2`.
	///
	/// If `include_fee` is set to `true`, the price will include the pool's fee.
	/// If the pool does not exist or the swap cannot be made, `None` is returned.
	fn quote_price_tokens_for_exact_tokens(
		asset1: Self::AssetKind,
		asset2: Self::AssetKind,
		amount: Self::Balance,
		include_fee: bool,
	) -> Option<Self::Balance>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1176-1188)
```rust
impl pallet_asset_conversion_tx_payment::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type AssetId = xcm::v5::Location;
	type OnChargeAssetTransaction = SwapAssetAdapter<
		WestendLocation,
		NativeAndNonPoolAssets,
		AssetConversion,
		ResolveAssetTo<StakingPot, NativeAndNonPoolAssets>,
	>;
	type WeightInfo = weights::pallet_asset_conversion_tx_payment::WeightInfo<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetConversionTxHelper;
}
```
