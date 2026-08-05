Audit Report

## Title
`SwapFirstAssetTrader::buy_weight` prices XCM fee purchases off an instantaneously-manipulable `pallet-asset-conversion` pool, letting an attacker underpay execution fees via same-transaction swap-then-reverse - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::buy_weight` computes the amount of a user-supplied asset needed to buy a fixed amount of `Target` (the XCM execution fee, from `WeightToFee::weight_to_fee`) by calling `SwapCredit::swap_tokens_for_exact_tokens`, which resolves to `pallet_asset_conversion`'s exact-output swap logic priced directly off the pool's current on-chain reserves. [1](#0-0)  Because the requested output amount (`fee`) is fixed and independent of the pool price, while the required input amount is computed from spot reserves with no TWAP or deviation bound, an attacker who shifts the pool's reserves immediately before invoking `buy_weight` and reverses the shift immediately after can reduce the input amount needed, extracting value from the pool's other liquidity providers, gated only by the pool's swap fee on the round-trip volume.

## Finding Description
`buy_weight` takes the first fungible asset in `payment`, wraps it as `credit_in`, and calls `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)` where `fee = WeightToFee::weight_to_fee(&weight)` is fixed regardless of pool state. [2](#0-1)  This resolves to `pallet_asset_conversion::Pallet::swap_tokens_for_exact_tokens`/`SwapCredit` implementation, whose exact-input calculation is driven by `get_reserves`/`get_balance`, i.e. the pool's live, mutable balances. [3](#0-2) 

The `QuotePrice` trait documentation explicitly states that any quote is only valid "if no other swaps are made ... before the target swap", confirming this is an unprotected spot price with no manipulation resistance. [4](#0-3)  The pallet's own `add_liquidity` documentation further confirms that reserves can be deliberately and atomically shifted via a batched call combining a liquidity operation with a swap ("batch an atomic call with `add_liquidity` and `swap_exact_tokens_for_tokens`... to rectify the exchange rate"), demonstrating the underlying primitive (atomic reserve manipulation within one transaction) is a known, reachable capability of this pallet, not merely theoretical. [5](#0-4) 

Because `buy_weight`'s target output (`fee`) is fixed rather than itself derived symmetrically from the same round-trip trade, an attacker can: (1) swap `Target` into the pool for `swap_asset` to increase `reserve(Target)`/decrease `reserve(swap_asset)`, (2) trigger `buy_weight` so `swap_tokens_for_exact_tokens` computes a reduced `amount_in` of `swap_asset` for the fixed `fee` output against the skewed reserves, and (3) reverse the initial swap to restore reserves, paying only the pool's swap fee on the round-trip volume. No existing check in `buy_weight` bounds the acceptable input amount against a reference/historical price, and no cross-block or cross-transaction reentrancy guard exists to prevent this sequencing.

## Impact Explanation
This targets the invariant that pool-held value (LP reserves in `pallet-asset-conversion`) must conserve value and settle at a fair rate; the described sequence lets an unprivileged attacker extract value from the pool's LPs by having their own fee purchase settle at a self-manufactured, non-market price, i.e. "public ... work" (XCM weight/fee) being paid for below its true cost, funded by uncompensated LP loss. This matches the "public underpriced work" / value-conservation impact categories in scope for `pallet-asset-conversion` and its consumers such as `SwapFirstAssetTrader`, which cumulus runtimes (Asset Hub Rococo/Westend, Penpal, staking-async parachain) wire in as a `WeightTrader`. [6](#0-5) 

## Likelihood Explanation
No privileged role is needed: any unprivileged account with capital sufficient to move a pool's reserves can perform the manipulate → buy_weight → reverse sequence atomically using ordinary public extrinsics (`swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens`) and a self-submitted `pallet_xcm::execute` with `BuyExecution`, batched via `pallet-utility::batch_all` or within a single XCM program. The precondition is a sufficiently shallow pool between the fee `Target` asset and the attacker's chosen `swap_asset`, which is plausible for newer or thinly-liquid asset pairs on Asset Hub. Profitability is bounded by the round-trip AMM fee versus the size of the fixed fee (`weight_to_fee`) being purchased and the pool depth, so the attack is most viable against shallow pools and/or higher-weight (higher-fee) XCM operations, but the underlying pricing mechanism provides no protection regardless of magnitude.

## Recommendation
- Do not settle `SwapFirstAssetTrader::buy_weight` purchases purely off the pool's instantaneous reserves; anchor pricing to a time-weighted or previous-block reference rate.
- Bound the acceptable `amount_in` in `buy_weight` against a governance-configured maximum deviation from a recent/historical quote, rejecting swaps priced outside that band.
- Consider disallowing the pool-manipulating swap extrinsics and the XCM fee-buying execution from being combined atomically for the same pool within one transaction/block (e.g., a one-swap-per-pool-per-block guard), analogous to flash-loan/reentrancy mitigations used elsewhere in DeFi.

## Proof of Concept
1. Identify or seed a thin `pallet-asset-conversion` pool `(swap_asset, Target)`.
2. In a single atomic transaction (e.g., `pallet_utility::batch_all`), call `swap_exact_tokens_for_tokens` to shift `reserve(swap_asset)` down and `reserve(Target)` up.
3. In the same transaction, submit `pallet_xcm::execute` carrying a `BuyExecution` in `swap_asset`, invoking `SwapFirstAssetTrader::buy_weight` [7](#0-6) , which calls `SwapCredit::swap_tokens_for_exact_tokens` against the now-skewed reserves and consumes less `swap_asset` than the pre-manipulation price would require for the fixed `fee` output.
4. Reverse the step-2 swap to restore reserves, recovering the bulk of the asset spent, minus the pool's swap fee, completing the cycle atomically with no lasting capital exposure. A Rust integration test in `cumulus/primitives/utility/src/tests/swap_first.rs` (or an asset-hub runtime integration test) can assert the pre- vs. post-manipulation `amount_in` values for the same `fee` output to demonstrate the discrepancy.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L425-489)
```rust
	fn buy_weight(
		&mut self,
		weight: Weight,
		mut payment: AssetsInHolding,
		_context: &XcmContext,
	) -> Result<AssetsInHolding, (AssetsInHolding, XcmError)> {
		log::trace!(
			target: "xcm::weight",
			"SwapFirstAssetTrader::buy_weight weight: {:?}, payment: {:?}",
			weight,
			payment,
		);
		let Some((id, given_credit)) = payment.fungible.first_key_value() else {
			return Err((payment, XcmError::AssetNotFound));
		};
		let id = id.clone();
		let given_credit_amount = given_credit.amount();
		let first_asset: Asset = (id.clone(), given_credit_amount).into();
		let Ok((fungibles_id, _)) = FungiblesAssetMatcher::matches_fungibles(&first_asset) else {
			log::trace!(
				target: "xcm::weight",
				"SwapFirstAssetTrader::buy_weight asset {:?} didn't match",
				first_asset,
			);
			return Err((payment, XcmError::AssetNotFound));
		};

		let swap_asset = fungibles_id.clone().into();
		if Target::get().eq(&swap_asset) {
			log::trace!(
				target: "xcm::weight",
				"SwapFirstAssetTrader::buy_weight Asset was same as Target, swap not needed.",
			);
			// current trader is not applicable.
			return Err((payment, XcmError::FeesNotMet));
		}
		// Subtract required from payment
		let Some(imbalance) = payment.fungible.remove(&first_asset.id) else {
			return Err((payment, XcmError::TooExpensive));
		};
		// "manually" build the concrete credit and move the imbalance there.
		let mut credit_in = fungibles::Credit::<AccountId, Fungibles>::zero(fungibles_id);
		credit_in.saturating_subsume(imbalance);

		let fee = WeightToFee::weight_to_fee(&weight);
		// swap the user's asset for the `Target` asset.
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
			Ok(a) => a,
			Err((credit_in, error)) => {
				log::trace!(
					target: "xcm::weight",
					"SwapFirstAssetTrader::buy_weight swap couldn't be done. Error was: {:?}",
					error,
				);
				// put back the taken credit
				let taken =
					AssetsInHolding::new_from_fungible_credit(id.clone(), Box::new(credit_in));
				payment.subsume_assets(taken);
				return Err((payment, XcmError::FeesNotMet));
			},
		};
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L459-462)
```rust
		/// NOTE: when encountering an incorrect exchange rate and non-withdrawable pool liquidity,
		/// batch an atomic call with [`Pallet::add_liquidity`] and
		/// [`Pallet::swap_exact_tokens_for_tokens`] or [`Pallet::swap_tokens_for_exact_tokens`]
		/// calls to render the liquidity withdrawable and rectify the exchange rate.
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```
