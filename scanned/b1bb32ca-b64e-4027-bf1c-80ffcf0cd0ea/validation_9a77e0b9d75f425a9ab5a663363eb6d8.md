## Analysis

I've confirmed the local analog. `SwapFirstAssetTrader::buy_weight` in `cumulus/primitives/utility/src/lib.rs` performs an on-chain AMM swap (`SwapCredit::swap_tokens_for_exact_tokens`) at the *current spot price* of a `pallet-asset-conversion` pool to convert an arbitrary XCM-supplied fee asset into the required `Target` fee asset, with no independent price ceiling — the only bound on the swap is the size of the payment itself, not a fair-price bound. This is used live in `asset-hub-rococo`, `asset-hub-westend`, `penpal`, and the `staking-async` parachain runtime as an XCM `WeightTrader`.

### Title
Unbounded spot-price AMM conversion in `SwapFirstAssetTrader::buy_weight` allows underpriced XCM weight purchase - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader::buy_weight` converts a user-supplied fee asset into the runtime's `Target` fee asset by calling `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)` [1](#0-0) . This swap is executed against the live reserves of the corresponding `pallet-asset-conversion` pool with no caller-supplied maximum-input/price bound beyond the size of the payment credit itself. If the pool's spot price has been skewed just before this call executes, the amount of the user's asset actually consumed to obtain the required `fee` amount of `Target` can be far below fair value.

### Finding Description
The trait `SwapCredit::swap_tokens_for_exact_tokens` signature takes only `path`, `credit_in`, and `amount_out` — no `amount_in_max` parameter [2](#0-1) . The underlying pallet implementation (`do_swap_credit_tokens_for_exact_tokens`) computes `amount_in` from the *current* pool reserves via `balance_path_from_amount_out` and only checks `amount_in <= amount_in_max` where `amount_in_max` is simply `credit_in.peek()`, i.e., however much the caller happened to hand over [3](#0-2) . There is no mechanism for `buy_weight` to reject a swap because the computed price is worse than some fair/expected value — it only fails if the payment is insufficient at whatever the *current* (possibly manipulated) price happens to be.

An attacker can, using their own ordinary transactions (no privileged access, no relayer/collator collusion required):
1. Perform a large `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` sequence on the `swap_asset`/`Target` pool to temporarily skew reserves so that a small amount of `swap_asset` quotes as sufficient for a large amount of `Target`.
2. While the pool is in this skewed state, submit an XCM message whose `BuyExecution` fee is paid in `swap_asset`, sized so that `SwapFirstAssetTrader::buy_weight` succeeds in swapping a tiny `credit_in` for the full `fee` (`WeightToFee::weight_to_fee(&weight)`) needed to cover a large amount of local execution weight.
3. Reverse the initial pool manipulation afterward (or let arbitrageurs restore it), recovering most of the capital used to skew the pool while having obtained weight/fee credit at a fraction of its true cost.

This is not a front-running attack against another party's transaction; it is self-contained price manipulation of a pool the attacker fully controls the sequencing of via their own extrinsics/XCM messages, then consuming that manipulated price via an on-chain public entrypoint (`buy_weight`) that has no independent slippage/price bound.

### Impact Explanation
This directly matches the "public underpriced work that degrades block production" impact category: an attacker can pay a small, manipulated amount of a chosen fee asset to obtain disproportionately large XCM execution weight allowance, letting them execute heavy `Transact`/`ExchangeAsset`/`DepositAsset` programs for a fraction of the intended cost. Repeated at scale, this allows cheap consumption of block weight, degrading effective throughput/availability for legitimate users on affected AssetHub/Penpal/staking-async parachains that configure `SwapFirstAssetTrader` as (part of) their `WeightTrader`.

### Likelihood Explanation
Likelihood depends on pool liquidity depth relative to the attacker's capital and on the swap/XCM execution landing within the same block or narrow window before arbitrage restores the price — the same precondition noted in the original HAL-01 report ("effectiveness … largely depends on the available liquidity"). Shallow, thinly-traded `swap_asset`/`Target` pools (which are common for many foreign-asset/native pairs on AssetHub) make this both cheap and repeatable. No validator, collator, relayer, or governance compromise is needed — only ordinary user-level transactions controlled entirely by the attacker.

### Recommendation
Bound `buy_weight`'s swap by an explicit, attacker-independent price reference (e.g., a time-weighted or previously-quoted price via `QuotePrice`, which the struct already requires as a trait bound but does not use inside `buy_weight`), rejecting the swap if the realized `amount_in` deviates beyond a configured tolerance from that reference quote — mirroring the recommendation in the original report to use an averaged price window or an off-chain-verified minimum rather than raw current-block spot reserves.

### Proof of Concept
1. Attacker holds `swap_asset` X and enough capital to briefly imbalance the `X`/`Target` pool on AssetHub (e.g., via `pallet_asset_conversion::swap_exact_tokens_for_tokens` or `add_liquidity`/`remove_liquidity` sequencing) so that spot price temporarily favors converting a small `X` amount into a large `Target` amount.
2. In the same block (or immediately after, before arbitrage), attacker submits an XCM message with `BuyExecution { fees: (X, small_amount), weight_limit: large_weight }`.
3. `SwapFirstAssetTrader::buy_weight` is invoked with `credit_in = small_amount` of `X`; `SwapCredit::swap_tokens_for_exact_tokens` succeeds because, under the skewed reserves, `amount_in <= credit_in.peek()` holds for the needed `fee` [3](#0-2) .
4. Attacker's XCM program then executes `large_weight` worth of instructions (e.g., nested `Transact` calls) for a fraction of the intended fee cost, and reverses the pool manipulation to recover capital.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-489)
```rust
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
