## Analysis

The external report's core issue: an unchecked return value from a token transfer call can silently produce an inconsistent state — funds appear moved but the actual settled amount is never verified, breaking the "transfer succeeded means value moved atomically" invariant. The closest local analog in `polkadot-sdk--036` is not in the ERC20-precompile/XCM transactor code (which does verify Solidity-style boolean returns, see `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`), but in the **default `fungibles::Mutate::transfer` implementation**, which discards the actual settled amounts from both legs of a `BestEffort` transfer and unconditionally reports success for the full requested amount.

### Title
Unchecked best-effort balance mutation results in default `fungibles::transfer` misreporting settled amount - (File: substrate/frame/support/src/traits/tokens/fungibles/regular.rs)

### Summary
The default trait implementation of `Mutate::transfer` in `substrate/frame/support/src/traits/tokens/fungibles/regular.rs` performs a source-side `decrease_balance` and a destination-side `increase_balance` using `Precision::BestEffort`, but discards both operations' returned "actual amount moved" values instead of checking them, and unconditionally returns `Ok(amount)` (the *requested* amount, not the *actual* amount transferred).

### Finding Description
`Mutate::transfer` is implemented as: [1](#0-0) 

```rust
fn transfer(...) -> Result<Self::Balance, DispatchError> {
    let _extra = Self::can_withdraw(asset.clone(), source, amount).into_result(preservation != Expendable)?;
    Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
    if source == dest { return Ok(amount); }

    Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
    // This should never fail as we checked `can_deposit` earlier. But we do a best-effort anyway.
    let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
    Self::done_transfer(asset, source, dest, amount);
    Ok(amount)
}
```

Both `decrease_balance` and `increase_balance` are `Unbalanced` trait methods that, with `Precision::BestEffort`, are permitted to move *less* than the requested `amount` (e.g., due to dust thresholds, existential-deposit rounding, or provider/consumer reference-count edge cases) and return the *actual* amount moved as their `Ok(Balance)` value. Here:
- `decrease_balance`'s returned actual-decreased amount is not captured (only the `Result`'s error variant is propagated via `?`; the `Ok` payload is dropped).
- `increase_balance`'s result is fully discarded with `let _ = ...`, silencing both the amount *and* any error.
- The function then returns `Ok(amount)` — the caller's requested amount — regardless of what was actually debited from `source` or credited to `dest`.

This is structurally identical to the reported bug class: an external/inner value-moving call's result is not checked, so the caller cannot tell whether the "weird edge case" (partial/best-effort settlement) occurred, and proceeds as if the full amount was conserved.

### Impact Explanation
If `decrease_balance` removes less than `amount` from `source` (permitted under `BestEffort`) while `increase_balance` also credits less than `amount` to `dest` (or fails silently), the emitted event via `done_transfer` and the returned balance both claim `amount` was moved, even though the source may have been debited a different quantity than the destination was credited. Any caller relying on this default trait method (used by fungibles-based Mutate consumers such as bridge/XCM asset-transactor glue code, pallet integrations built on `fungibles::Mutate`, and generic pallet code that calls `<T as fungibles::Mutate<_>>::transfer`) inherits an operation that does not conserve value and does not surface partial failure, potentially causing double-counted issuance, silently lost balance, or inconsistent total-issuance accounting for asset classes whose `Unbalanced` implementation can legitimately return less than requested under `BestEffort`.

### Likelihood Explanation
This is the *default* implementation shipped in `frame_support`'s fungibles trait module, meaning any `fungibles::Mutate` implementer that does not override `transfer` (or that inherits this default via auto-trait derivation) is exposed. The condition is triggered purely by normal best-effort semantics (near-ED balances, dust sweeping) — no privileged actor, relayer, or governance action is required; a routine transfer near the existential deposit boundary is sufficient to exercise the discrepancy.

### Recommendation
Capture and reconcile the actual amounts returned by both `decrease_balance` and `increase_balance`; propagate/log any shortfall, and return the minimum of the two actual amounts (or an error) instead of the originally requested `amount`, mirroring the SafeERC20 recommendation of checking every settlement leg's real result before reporting success.

### Proof of Concept
Conceptual reproduction (would need to be exercised against a concrete `fungibles::Mutate` implementer that inherits this default and whose `Unbalanced::decrease_balance`/`increase_balance` can return less than requested under `Precision::BestEffort`, e.g. an asset near its minimum balance / existential deposit):
1. Fund `source` with `amount + minimum_balance - 1` of `asset` such that a full `amount` withdrawal under `BestEffort` triggers dust-removal semantics and only `amount - dust` is actually removed.
2. Call `<T as fungibles::Mutate<_>>::transfer(asset, &source, &dest, amount, Preservation::Expendable)`.
3. Observe the function returns `Ok(amount)` and emits `done_transfer(asset, source, dest, amount)`, while the true balance delta on `source`/`dest` differs from `amount`, because both inner calls' actual-moved values were discarded (`decrease_balance(...)?` drops its `Ok` payload, `let _ = increase_balance(...)` drops everything). [2](#0-1)

### Citations

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
