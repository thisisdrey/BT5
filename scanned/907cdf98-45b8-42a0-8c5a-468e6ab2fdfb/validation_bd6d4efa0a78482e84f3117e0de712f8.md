## Title
Unprivileged swap can burn a user's input asset for zero output in `pallet-asset-conversion` when `amount_out_min` is omitted - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`do_swap_exact_tokens_for_tokens` validates that `amount_in > 0`, but never validates that the computed `amount_out` is non-zero before executing the transfer. In a skewed pool, a tiny `amount_in` can round `get_amount_out` down to `0` via integer division. If the caller does not supply `amount_out_min` (it is `Option<T::Balance>`), the swap proceeds, the input asset is withdrawn from the caller, and the output amount transferred to `send_to` is `0` — the mirror image of the reported `sellForLP` bug (burn input, mint/emit nothing in return).

### Finding Description
`do_swap_exact_tokens_for_tokens` [1](#0-0)  only checks `amount_in > Zero::zero()` and, if present, `amount_out_min > Zero::zero()`. It then computes the output via `balance_path_from_amount_in`, which calls `get_amount_out` [2](#0-1)  — this only errors when a *reserve* is zero, not when the *computed output* rounds to zero via `checked_div`. If `amount_out_min` is `None`, the resulting `amount_out == 0` is never rejected before `Self::swap(&sender, &path, &send_to, keep_alive)?` executes.

By contrast, the pallet's *quote* helpers (`quote_price_exact_tokens_for_tokens` / `quote_price_tokens_for_exact_tokens`) were explicitly hardened in this repository to reject zero-rounded output (see PR 11795) [3](#0-2) , and the same "tiny input rounds to zero in a skewed pool" scenario is demonstrated in the test suite [4](#0-3) . However, that hardening was applied only to the read-only quote path, not to the actual swap-execution path (`do_swap_exact_tokens_for_tokens`), leaving the real extrinsic still able to execute a swap that withdraws the caller's asset and delivers zero of the counter-asset.

### Impact Explanation
This is a public, unprivileged-entrypoint fund-loss primitive: any account calling `swap_exact_tokens_for_tokens` (via the pallet's dispatchable, without specifying `amount_out_min`) against a heavily skewed pool with a small enough `amount_in` will have their input asset withdrawn while receiving `0` of the output asset — value is destroyed rather than conserved, matching the "Balances ... must conserve value and settle exactly once" pivot. It is directly analogous to the reported `sellForLP` bug: input consumed, output computed as zero due to unguarded division/rounding, no revert.

### Likelihood Explanation
Likelihood is limited by economics/UX: `amount_out_min` is optional and many wallets/UIs default to specifying a nonzero slippage-protected minimum, and the loss per transaction is bounded by the (tiny) `amount_in` used to trigger the zero-rounding, so this is more a footgun/dust-loss bug than a large-scale drain. It requires a skewed/thin pool and a caller who omits `amount_out_min`, both attacker-controlled/attacker-creatable conditions, requiring no privileged role, relayer, or governance action.

### Recommendation
Add an explicit zero-output check in `do_swap_exact_tokens_for_tokens` (and any other real swap-execution path building off `balance_path_from_amount_in`/`balance_path_from_amount_out`) mirroring the guard already added to the quote functions, e.g. `ensure!(!amount_out.is_zero(), Error::<T>::ZeroAmount);` immediately after computing `amount_out`, before calling `Self::swap`.

### Proof of Concept
1. Create a pool and add heavily skewed liquidity, e.g. `add_liquidity(asset1=1_000_000, asset2=200)` — the same setup used in `quote_price_returns_none_for_zero_output` [4](#0-3) .
2. Call `swap_exact_tokens_for_tokens` (the dispatchable wrapping `do_swap_exact_tokens_for_tokens`) with `amount_in = 1`, `path = [asset1, asset2]`, and `amount_out_min = None`.
3. `balance_path_from_amount_in` computes `get_amount_out(fee, 1, 1_000_000, 200) = 0` (as commented in the test: `1*997*200 / (1_000_000*1000 + 997) = 0`).
4. Because `amount_out_min` is `None`, the `ensure!(amount_out >= amount_out_min, ...)` check at line 997-1002 is skipped entirely, and `Self::swap` executes: the caller's `1` unit of `asset1` is withdrawn, and `0` units of `asset2` are credited to `send_to` — the caller's input is consumed for nothing.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L980-1014)
```rust
		pub(crate) fn do_swap_exact_tokens_for_tokens(
			sender: T::AccountId,
			path: Vec<T::AssetKind>,
			amount_in: T::Balance,
			amount_out_min: Option<T::Balance>,
			send_to: T::AccountId,
			keep_alive: bool,
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

			Self::swap(&sender, &path, &send_to, keep_alive)?;

			Self::deposit_event(Event::SwapExecuted {
				who: sender,
				send_to,
				amount_in,
				amount_out,
				path,
			});
			Ok(amount_out)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1384-1419)
```rust
		/// Calculates amount out for a given swap `fee`.
		///
		/// Given an input amount of an asset and pair reserves, returns the maximum output amount
		/// of the other asset.
		pub fn get_amount_out(
			fee: Permill,
			amount_in: &T::Balance,
			reserve_in: &T::Balance,
			reserve_out: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			let amount_in = T::HigherPrecisionBalance::from(*amount_in);
			let reserve_in = T::HigherPrecisionBalance::from(*reserve_in);
			let reserve_out = T::HigherPrecisionBalance::from(*reserve_out);

			if reserve_in.is_zero() || reserve_out.is_zero() {
				return Err(Error::<T>::ZeroLiquidity);
			}

			let fee_complement = fee.left_from_one().deconstruct();
			let amount_in_with_fee = amount_in
				.checked_mul(&T::HigherPrecisionBalance::from(fee_complement))
				.ok_or(Error::<T>::Overflow)?;

			let numerator =
				amount_in_with_fee.checked_mul(&reserve_out).ok_or(Error::<T>::Overflow)?;

			let denominator = reserve_in
				.checked_mul(&T::HigherPrecisionBalance::from(Permill::ACCURACY))
				.ok_or(Error::<T>::Overflow)?
				.checked_add(&amount_in_with_fee)
				.ok_or(Error::<T>::Overflow)?;

			let result = numerator.checked_div(&denominator).ok_or(Error::<T>::Overflow)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1549-1552)
```rust
			// Small inputs can round output to zero due to integer division.
			if amount_out.is_zero() {
				return None;
			}
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L716-738)
```rust
		// Create a heavily skewed pool: lots of asset1, very little asset2.
		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone()),
			1_000_000,
			200,
			1,
			1,
			user,
		));

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
