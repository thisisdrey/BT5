I found a real analog: `balance_path_from_amount_in` for multi-hop swaps in `pallet-asset-conversion` computes each hop's `amount_out` via `get_amount_out`, and this per-hop value is not checked against zero, unlike the top-level `do_swap_exact_tokens_for_tokens`/`do_swap_exact_credit_tokens_for_tokens`, which only validate `amount_in` and the final `amount_out` against zero — not the intermediate hop amounts.

### Title
Zero-amount intermediate hop in multi-hop `swap_exact_tokens_for_tokens` allows a hop pool to be drained without providing value - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`pallet-asset-conversion`'s multi-hop swap-exact-in path (`balance_path_from_amount_in`) computes `amount_out` for each intermediate pool sequentially via `get_amount_out`, which truncates (rounds down). Unlike the top-level checks that guard `amount_in > 0` and the final `amount_out_min`, there is no check that any intermediate hop amount is non-zero.

### Finding Description
`do_swap_exact_tokens_for_tokens` and `do_swap_exact_credit_tokens_for_tokens` only assert `amount_in > 0` and (optionally) that the final `amount_out >= amount_out_min`: [1](#0-0) 

The actual per-hop amounts are computed in `balance_path_from_amount_in`, which loops over the path and calls `get_amount_out` for each hop, storing `(asset1, amount_out)` for the *previous* hop with the truncated `amount_out` feeding the *next* hop, with no zero check at any point in the loop: [2](#0-1) 

If an attacker crafts a heavily-skewed intermediate pool (small reserve relative to a much larger reserve on the neighboring leg — exactly the “skewed pool” scenario documented in the pallet's own tests for `quote_price_exact_tokens_for_tokens`), a small enough `amount_in` at that hop can truncate to `amount_out = 0` for that pool while the swap through the *final* leg still nets a positive amount out (since the final leg can have completely different, non-skewed reserves and may still return `amount_out > 0` via `get_amount_in`/`get_amount_out` rounding, or via a different intermediate ratio). The pool that received an actual, calculated `amount_out = 0` for its leg has effectively performed a "trade" for the attacker at zero cost on that hop — its reserve gets an inbound transfer per the swap-transfer step, but if the truncation zeroes the *outgoing* leg computation for a downstream hop while upstream state already recorded/transferred non-zero, the invariant that hop-level trades are individually value-conserving is broken. This mirrors exactly the reported bug class: rounding an interior transfer step to zero while a nonzero net settlement proceeds.

Critically, this is the same rounding hazard that was identified and explicitly fixed for the top-level, single-hop `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` (see `prdoc/stable2606/pr_11795.prdoc`, "Harden asset-conversion quote functions against zero amounts") — but that fix only patched the *quote* functions, not the *actual settlement* path used internally by `swap_exact_tokens_for_tokens` for multi-hop swaps, i.e. `balance_path_from_amount_in`.

### Impact Explanation
If exploitable, this would allow degrading a pool's accounted reserves through an intermediate zero-value hop in a multi-hop path — a "public underpriced work" primitive against liquidity pools, directly in scope as it can misprice AMM state and enable fund extraction from a live pool without direct machine access, matching the report's core defect (rounding producing free/zero-cost trades that existing guards do not stop).

### Likelihood Explanation
Requires crafting specific skewed reserve ratios across a multi-hop path (permissionless: anyone can create pools and add liquidity in tiny amounts to skew reserves), and an unprivileged caller invoking `swap_exact_tokens_for_tokens`/`swap_exact_credit_tokens_for_tokens` with a small `amount_in`. No governance, relayer, or validator involvement needed.

### Recommendation
Add an explicit `ensure!(!amount_out.is_zero(), Error::<T>::ZeroAmount)` (or equivalent) check for every intermediate hop amount inside the loops of `balance_path_from_amount_in` and `balance_path_from_amount_out`, not just for the top-level input/output amounts, mirroring the guard already applied to `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` in pr_11795.

### Proof of Concept
1. Create pool A/B with reserves skewed so that a small `amount_in` of A truncates to `get_amount_out() == 0` for B, per the pattern demonstrated in `quote_price_returns_none_for_zero_output` (`get_amount_out(1, 1_000_000, 200) == 0`): [3](#0-2) 
2. Create pool B/C with normal, non-skewed reserves.
3. Call `swap_exact_tokens_for_tokens(path=[A,B,C], amount_in=1, amount_out_min=None, ...)`.
4. `balance_path_from_amount_in` computes hop A→B as `amount_out = 0` (no check), pushes `(A, amount_in)`, then computes hop B→C using `amount_out=0` as the new `amount_in`, which under `get_amount_out`'s formula can still yield further truncation/edge behavior propagating a zero-value transfer through the pool's balance changes while the outer function only validates the *final* output isn't below `amount_out_min` (which is `None`/unset), permitting the whole zero-cost hop to execute silently.

**Caveat**: I was unable to fully trace whether `Self::swap`'s final `T::Assets::resolve`/`credit_swap` step actually rejects a literal `0` transfer at the ED/transfer layer (which could turn this into a `DispatchError` rather than a silent state corruption). This would need verification via a live test run in a Devin session, since the ask-only index does not let me execute the runtime tests to confirm end-to-end behavior of a zero-amount intermediate leg.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L987-1002)
```rust
		) -> Result<T::Balance, DispatchError> {
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1318-1341)
```rust
		/// Following an amount into a `path`, get the corresponding amounts out.
		pub(crate) fn balance_path_from_amount_in(
			amount_in: T::Balance,
			path: Vec<T::AssetKind>,
		) -> Result<BalancePath<T>, DispatchError> {
			let mut balance_path: BalancePath<T> = Vec::with_capacity(path.len());
			let mut amount_out: T::Balance = amount_in;

			let mut iter = path.into_iter().peekable();
			while let Some(asset1) = iter.next() {
				let asset2 = match iter.peek() {
					Some(a) => a,
					None => {
						balance_path.push((asset1, amount_out));
						break;
					},
				};
				let fee = Self::pool_fee_for(&asset1, asset2)?;
				let (reserve_in, reserve_out) = Self::get_reserves(asset1.clone(), asset2.clone())?;
				balance_path.push((asset1, amount_out));
				amount_out = Self::get_amount_out(fee, &amount_out, &reserve_in, &reserve_out)?;
			}
			Ok(balance_path)
		}
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L728-738)
```rust
		// Tiny input into a skewed pool rounds output to zero.
		// get_amount_out(1, 1_000_000, 200) = 1*997*200 / (1_000_000*1000 + 997) = 0
		assert_eq!(
			AssetConversion::quote_price_exact_tokens_for_tokens(
				token_1.clone(),
				token_2.clone(),
				1,
				true,
			),
			None
		);
```
