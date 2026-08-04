### Title
`fungible`/`fungibles::Mutate::transfer` default implementation returns the full requested amount even though the underlying move is `BestEffort` and its actual result is discarded - ([File: substrate/frame/support/src/traits/tokens/fungible/regular.rs])

### Summary
The default `transfer` implementation on the `fungible::Mutate` and `fungibles::Mutate` traits calls `decrease_balance(..., BestEffort, ...)` and `increase_balance(..., BestEffort)`, but discards both actual-amount return values and unconditionally returns `Ok(amount)` — i.e. it always reports "the full requested amount moved" even when the internal move was best-effort and could move less. This is the same broken invariant as the `AssetManager::withdraw` bug in the external report: a value-moving primitive signals full success while under-delivering, and every caller that trusts the `Ok(amount)` return to update its own accounting can silently desynchronize from real balances.

### Finding Description
`fungible::Mutate::transfer` (and its `fungibles` counterpart) is defined as: [1](#0-0) 

```
fn transfer(
    source: &AccountId,
    dest: &AccountId,
    amount: Self::Balance,
    preservation: Preservation,
) -> Result<Self::Balance, DispatchError> {
    let _extra = Self::can_withdraw(source, amount).into_result(preservation != Expendable)?;
    Self::can_deposit(dest, amount, Extant).into_result()?;
    if source == dest {
        return Ok(amount);
    }

    Self::decrease_balance(source, amount, BestEffort, preservation, Polite)?;
    // This should never fail as we checked `can_deposit` earlier. But we do a best-effort
    // anyway.
    let _ = Self::increase_balance(dest, amount, BestEffort);
    Self::done_transfer(source, dest, amount);
    Ok(amount)
}
```

The identical pattern exists for `fungibles::Mutate::transfer`: [2](#0-1) 

Key observations:
- `decrease_balance` is explicitly documented to be `BestEffort`-capable: "If `precision` is `BestEffort`, then reduce the balance of `who` by the most that is possible, up to `amount`… if `Ok` is returned then the inner is the amount by which it was reduced" [3](#0-2) . The `transfer()` wrapper never inspects this returned "actual amount reduced" — it just propagates `?` for the `Err` case and throws away the `Ok(actual)` value.
- `increase_balance`'s result is discarded with `let _ =`, so even a deposit shortfall (e.g. hitting `minimum_balance` rounding under `BestEffort`, see lines 205-233 of the same file) is silently swallowed.
- Regardless of what the primitive actually moved, `transfer()` always returns `Ok(amount)` — the *requested* amount, not the *actual* amount moved.
- The upstream `can_withdraw`/`can_deposit` pre-checks use different semantics than the ones ultimately used inside `decrease_balance`'s `reducible_balance(preservation, force)` call (`can_withdraw` in the trait takes no `preservation`/`force` parameters at all), so a caller-selected `Preservation::Preserve` requirement can produce a smaller `reducible_balance` at the `decrease_balance` step than what the earlier `can_withdraw` check validated, letting the `BestEffort` decrease move less than `amount` while the function still reports full success.

This exactly reproduces the reported bug class: a fund-moving function that internally tracks a "remaining"/"actual moved" amount but returns success (`Ok(amount)`/`true`) without checking that the full requested value was moved.

Contrast this with the codebase's own awareness of the risk: `substrate/frame/asset-conversion/ops/src/lib.rs` explicitly guards against exactly this failure mode by asserting the returned value equals the expected balance: [4](#0-3) 
```
ensure!(
    balance1 ==
        T::Assets::transfer(
            asset1.clone(),
            &prior_account,
            &new_account,
            balance1,
            Preservation::Expendable,
        )?,
    Error::<T>::PartialTransfer
);
```
The fact that this defensive `ensure!` was needed shows the trait's own contract is not safe to trust blindly — but this check is opt-in per call site, not enforced by the trait, so many other call sites across the codebase invoke `Mutate::transfer`/`fungibles::Mutate::transfer` and use the returned `Ok(_)` (or simply `?`) as proof the full `amount` moved, without re-verifying the returned balance.

### Impact Explanation
Any pallet or runtime logic that calls `fungible::Mutate::transfer` / `fungibles::Mutate::transfer` and treats `Ok(_)` as "the full amount was moved" (rather than checking the returned value) can end up crediting a beneficiary's accounting (burn/mint records, pool shares, deposit trackers, reward ledgers) for more than was actually delivered whenever the source account's `reducible_balance` under the caller's requested `Preservation`/lock state is less than the nominal balance check performed by `can_withdraw`. This can lead to: value not conserved between source and destination, under-delivery to a beneficiary while the caller's bookkeeping assumes full delivery, and permanent value loss/lock in edge cases involving locks, freezes, or holds that reduce `reducible_balance` below the checked amount. This matches "Balances, assets… must conserve value and settle exactly once to the rightful beneficiary and amount" from the impact gate.

### Likelihood Explanation
Moderate-to-low without further verification. Exploitability depends on finding a concrete call site where (a) the caller does not additionally assert the returned balance equals the requested amount (unlike `asset-conversion/ops`), and (b) a lock/freeze/hold interacts with `Preservation`/`Fortitude::Polite` such that `can_withdraw`'s pre-check and `decrease_balance`'s actual `reducible_balance` diverge. I was not able to fully trace every caller of `fungible::Mutate::transfer`/`fungibles::Mutate::transfer` in the codebase within the available iterations to confirm a specific unguarded call site that both discards the return value and is reachable by an unprivileged account, so this should be treated as a confirmed *broken primitive contract* with a *plausible but not fully proven* concrete exploitation path.

### Recommendation
- Change `Mutate::transfer` (both `fungible` and `fungibles`) to capture the actual values returned by `decrease_balance` and `increase_balance` and return the true moved amount (or `Err` if it is less than requested and `Precision::Exact` semantics are desired), rather than unconditionally returning `Ok(amount)`.
- Alternatively, expose a `Precision` parameter on `transfer()` itself, defaulting to `Exact`, so callers that need best-effort semantics must opt in explicitly and callers that need guaranteed full transfer get a hard error rather than silent under-delivery.
- Audit call sites that use `Mutate::transfer`/`fungibles::Mutate::transfer` without an explicit `ensure!(returned_amount == expected)` check (as already done defensively in `asset-conversion/ops`) and add such checks or propagate a dedicated `PartialTransfer` error.

### Proof of Concept
Conceptual reproduction (cannot fully execute without a live test harness in this pass):
1. Configure an account `A` with balance `B` and a `freeze`/`hold` of size `F` such that `reducible_balance(A, Preservation::Preserve, Polite) = B - F - ED` is smaller than what an upstream `can_withdraw(A, amount)` check (which does not take `preservation`) validates as available.
2. Call `<T::Currency as fungible::Mutate<_>>::transfer(&A, &dest, amount, Preservation::Preserve)` with `amount` chosen to pass `can_withdraw` but exceed the `Preserve`-constrained `reducible_balance`.
3. Observe: `decrease_balance` (BestEffort) moves less than `amount` from `A`, `increase_balance` credits `dest` with the possibly-smaller actual amount (further truncated by `BestEffort` minimum-balance rounding), yet `transfer()` returns `Ok(amount)` — the full requested value — misleading any caller that trusts the return value for its own accounting (e.g., increasing a pool/LP balance or marking a payout as fully completed) by the shortfall `amount - actual_moved`.

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L168-197)
```rust
	/// Reduce the balance of `who` by `amount`.
	///
	/// If `precision` is [`Exact`] and it cannot be reduced by that amount for
	/// some reason, return `Err` and don't reduce it at all. If `precision` is [`BestEffort`], then
	/// reduce the balance of `who` by the most that is possible, up to `amount`.
	///
	/// In either case, if `Ok` is returned then the inner is the amount by which is was reduced.
	/// Minimum balance will be respected and thus the returned amount may be up to
	/// [`Inspect::minimum_balance()`] - 1` greater than `amount` in the case that the reduction
	/// caused the account to be deleted.
	fn decrease_balance(
		who: &AccountId,
		mut amount: Self::Balance,
		precision: Precision,
		preservation: Preservation,
		force: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let old_balance = Self::balance(who);
		let reducible = Self::reducible_balance(who, preservation, force);
		match precision {
			BestEffort => amount = amount.min(reducible),
			Exact => ensure!(reducible >= amount, TokenError::FundsUnavailable),
		}

		let new_balance = old_balance.checked_sub(&amount).ok_or(TokenError::FundsUnavailable)?;
		if let Some(dust) = Self::write_balance(who, new_balance)? {
			Self::handle_dust(Dust(dust));
		}
		Ok(old_balance.saturating_sub(new_balance))
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L321-339)
```rust
	fn transfer(
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(source, amount).into_result(preservation != Expendable)?;
		Self::can_deposit(dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(dest, amount, BestEffort);
		Self::done_transfer(source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L366-386)
```rust
	fn transfer(
		asset: Self::AssetId,
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(asset.clone(), source, amount)
			.into_result(preservation != Expendable)?;
		Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
		Self::done_transfer(asset, source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/asset-conversion/ops/src/lib.rs (L209-219)
```rust
			ensure!(
				balance1 ==
					T::Assets::transfer(
						asset1.clone(),
						&prior_account,
						&new_account,
						balance1,
						Preservation::Expendable,
					)?,
				Error::<T>::PartialTransfer
			);
```
