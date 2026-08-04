## Analog Found

### Title
Silently-Ignored `increase_balance` Result in `fungibles::Mutate::transfer` Can Burn Debited Funds - (`substrate/frame/support/src/traits/tokens/fungibles/regular.rs`)

### Summary
The Index Protocol bug's core invariant is: *an ERC20-style transfer call can return a failure signal, but the caller discards that signal and treats the transfer as successful, leaving funds debited from the source and never credited to the destination.* The same pattern exists in the default `transfer` implementation of the `fungibles::Mutate` trait in `frame_support`, used across FRAME pallets and pallet configurations that move fungible-asset balances programmatically (e.g. XCM asset transactors, custom pallets built on `fungibles::Mutate`).

### Finding Description
`Pallet::transfer` in `fungibles::Mutate` performs a checked debit followed by an *unchecked* credit: [1](#0-0) 

```rust
fn transfer(...) -> Result<Self::Balance, DispatchError> {
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

- `decrease_balance` on `source` is checked (`?`) — if it fails, the whole call aborts and no state is changed.
- `increase_balance` on `dest` is invoked with `BestEffort` and its `Result` is explicitly discarded via `let _ = ...`. `BestEffort` means the deposit is allowed to be partially applied (silently capped) rather than reverted.
- Regardless of what actually happened on the `dest` side, the function unconditionally calls `done_transfer` (which emits the success event/hook) and returns `Ok(amount)` — the *requested* amount, not the amount that was actually credited.

The inline comment ("This should never fail ... but we do a best-effort anyway") is the exact analog of the Sherlock report's warning: an ERC20-style operation is assumed to always succeed, so its result is ignored, but the code path acknowledges that failure is actually possible.

Unlike `pallet-assets::do_transfer` (which mutates both balances inside a single `try_mutate` transaction and therefore atomically fails/rolls back together), this generic trait method performs the debit and credit as two independent, non-atomic operations, with the second one's outcome unchecked.

### Impact Explanation
Any component that uses the default `fungibles::Mutate::transfer` implementation (rather than an asset-specific atomic override) to move value between accounts is exposed to loss-of-funds:
- If `increase_balance` on `dest` fails or only partially succeeds after `decrease_balance` on `source` already succeeded (e.g. destination minimum-balance/ED constraints, freezer/holder callbacks triggered as a side effect of the `decrease_balance` call changing state between the `can_deposit` pre-check and the actual `increase_balance`, or balance-type overflow under `BestEffort` saturation), the debited amount is permanently lost: it leaves the source account but is not (fully) credited to the destination.
- The function still reports success (`Ok(amount)`), so any caller (extrinsic, XCM executor, another pallet) built on this trait will believe the transfer completed in full, propagating a false success state — exactly the "user does not receive tokens but system believes it succeeded" scenario described in the original report.
- This breaks the "Balances/assets ... must conserve value and settle exactly once to the rightful beneficiary and amount" invariant.

### Likelihood Explanation
The pre-check (`can_deposit`) is intended to make the subsequent `increase_balance` call safe, and in most straightforward configurations it will indeed succeed. However, the two calls are not atomic with the pre-check: `decrease_balance` can trigger dust-handling, freezer/holder hooks, or other side effects (depending on the concrete `fungibles::Mutate`/`Unbalanced` implementation) that could change the state of `dest` between `can_deposit` and `increase_balance`, and `BestEffort` explicitly permits partial/saturating application instead of failing outright. Because the failure mode is silent (ignored `Result`) and not exercised by a fail-fast assertion or `defensive!`, it is unlikely to be caught by tests that only cover the "happy path," matching the Sherlock report's core complaint that a transfer function assumed to never fail is still used without checking its result.

### Recommendation
Do not discard the result of `increase_balance`. Either:
- Use a checked (`Exact`/non-`BestEffort`) increase and propagate any error, rolling back the prior `decrease_balance` (e.g. wrap both in `with_transaction`/`try_mutate` so the whole transfer is atomic), or
- If `BestEffort` behavior is intentional, capture the actually-credited amount returned by `increase_balance` and return that (instead of the originally requested `amount`) so callers cannot be misled into believing the full amount was transferred.

### Proof of Concept
Conceptually:
1. Implement (or use) a `fungibles::Mutate` backend where `increase_balance` can fail/partially apply after `can_deposit` passed — e.g. a hold/freeze hook attached to `dest` that becomes active as a side effect of debiting `source` (shared state, rate limiter, or a `Freezer`/`Holder` callback keyed on aggregate balances) — or one where `BestEffort` on `increase_balance` saturates instead of adding the full amount (e.g. destination balance close to `Balance::MAX`, or a per-account cap enforced only inside `increase_balance`).
2. Call `<Pallet as fungibles::Mutate<AccountId>>::transfer(asset, &source, &dest, amount, preservation)`.
3. Observe: `source` balance decreases by `amount` (checked path succeeded), `dest` balance increases by less than `amount` (or not at all), yet the function returns `Ok(amount)` and `done_transfer` emits the transfer event/hook as if the full amount succeeded.
4. Downstream callers relying on the `Ok` return value (or the emitted event) will report the transfer as fully successful, while `amount - actual_credited` is permanently lost from total accounted balances between the two accounts. [2](#0-1)

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L363-386)
```rust
	///
	/// A transfer where the source and destination account are identical is treated as No-OP after
	/// checking the preconditions.
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
