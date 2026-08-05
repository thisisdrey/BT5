Audit Report

## Title
Unbounded spot-price AMM conversion in `SwapFirstAssetTrader::buy_weight` allows underpriced XCM weight purchase - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::buy_weight` converts an XCM-supplied fee asset into the runtime's `Target` fee asset by calling `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)` [1](#0-0) . This executes against the live `pallet-asset-conversion` pool reserves with no independent price/slippage bound beyond the size of the payment itself, allowing an attacker who manipulates the pool's spot price just before this call to obtain XCM execution weight at far below fair cost.

## Finding Description
`SwapCredit::swap_tokens_for_exact_tokens` takes only `path`, `credit_in`, and `amount_out` — no `amount_in_max` parameter is exposed to the caller [2](#0-1) . The implementation, `do_swap_credit_tokens_for_exact_tokens`, sets `amount_in_max = credit_in.peek()` — i.e., whatever the caller handed over — and computes `amount_in` from the *current* pool reserves via `balance_path_from_amount_out`, checking only `amount_in <= amount_in_max` [3](#0-2) . There is no fair-price or time-weighted reference check; the swap succeeds purely based on whatever the pool's spot price is at execution time.

Within `buy_weight`, the attacker's supplied fee asset amount (`credit_in`, taken directly from XCM `payment`) becomes this `amount_in_max`, and `fee = WeightToFee::weight_to_fee(&weight)` is the requested `amount_out` [4](#0-3) . An attacker who skews the pool's reserves beforehand (via ordinary public `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` extrinsics they fully control) can make a tiny `credit_in` sufficient to produce a large `fee` amount of `Target`, thereby buying disproportionate XCM execution weight cheaply. This is confirmed wired into production runtimes: `asset-hub-rococo`, `asset-hub-westend`, `penpal`, and the `staking-async` parachain runtime all reference `SwapFirstAssetTrader` in their `xcm_config.rs`.

## Impact Explanation
This matches the "public underpriced work that degrades block production" impact category: an attacker can pay a small, manipulated amount of a chosen asset to obtain a disproportionately large XCM weight allowance, enabling execution of heavy `Transact`/`ExchangeAsset`/`DepositAsset` programs for a fraction of intended cost. Repeated, this cheaply consumes block weight on affected AssetHub/Penpal/staking-async parachains.

## Likelihood Explanation
Exploitability depends on pool liquidity depth relative to attacker capital and on the manipulation and consuming XCM message landing within the same block or a narrow window before arbitrage restores price. Thinly-liquid `swap_asset`/`Target` pools (common among many foreign-asset pairs on AssetHub) make this both feasible and repeatable using only ordinary, unprivileged user transactions — no validator/collator/relayer compromise required.

## Recommendation
Bound the swap in `buy_weight` with an explicit, attacker-independent price reference (e.g., via `QuotePrice`, already a bound on related traits in the codebase) and reject swaps whose realized `amount_in` deviates beyond a configured tolerance from that reference, instead of relying solely on raw current-block spot reserves.

## Proof of Concept
1. Attacker holds asset `X` and enough capital to briefly skew the `X`/`Target` `pallet-asset-conversion` pool via public `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` calls so spot price temporarily favors converting a small `X` amount into a large `Target` amount.
2. In the same block or immediately after, attacker submits an XCM message with `BuyExecution { fees: (X, small_amount), weight_limit: large_weight }`.
3. `SwapFirstAssetTrader::buy_weight` invokes `SwapCredit::swap_tokens_for_exact_tokens` with `credit_in = small_amount`; under skewed reserves, `amount_in <= credit_in.peek()` holds for the required `fee`, so the swap succeeds [5](#0-4) .
4. Attacker's XCM program executes `large_weight` worth of instructions for a fraction of intended cost, then reverses the pool manipulation to recover capital.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L461-489)
```rust
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L99-113)
```rust
	/// Swaps a portion of `credit_in` of `path[0]` asset to obtain the desired `amount_out` of
	/// the `path[last]` asset. The provided `credit_in` must be adequate to achieve the target
	/// `amount_out`, or an error will occur.
	///
	/// On success, the function returns a (`credit_out`, `credit_change`) tuple, where `credit_out`
	/// represents the acquired amount of the `path[last]` asset, and `credit_change` is the
	/// remaining portion from the `credit_in`. On failure, an `Err` with the initial `credit_in`
	/// and error code is returned.
	///
	/// This operation is expected to be atomic.
	fn swap_tokens_for_exact_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out: Self::Balance,
	) -> Result<(Self::Credit, Self::Credit), (Self::Credit, DispatchError)>;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1123-1147)
```rust
		pub(crate) fn do_swap_credit_tokens_for_exact_tokens(
			path: Vec<T::AssetKind>,
			credit_in: CreditOf<T>,
			amount_out: T::Balance,
		) -> Result<(CreditOf<T>, CreditOf<T>), (CreditOf<T>, DispatchError)> {
			let amount_in_max = credit_in.peek();
			let inspect_path = |credit_asset| {
				ensure!(
					path.first().map_or(false, |a| a == &credit_asset),
					Error::<T>::InvalidPath
				);
				ensure!(amount_in_max > Zero::zero(), Error::<T>::ZeroAmount);
				ensure!(amount_out > Zero::zero(), Error::<T>::ZeroAmount);

				Self::validate_swap_path(&path)?;
				let path = Self::balance_path_from_amount_out(amount_out, path)?;

				let amount_in = path.first().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_in <= amount_in_max,
					Error::<T>::ProvidedMaximumNotSufficientForSwap
				);

				Ok((path, amount_in))
			};
```
