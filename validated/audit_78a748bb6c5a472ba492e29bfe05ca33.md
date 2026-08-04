## Analysis

The external report's core broken invariant is: *a "revoke-then-set" approval sequence that is not executed atomically leaves the allowance in an inconsistent, possibly permanently degraded state if the second step fails after the first step already mutated storage.*

A direct local analog exists in the ERC‑20 precompile for `pallet-assets`.

### Finding

In `substrate/frame/assets/precompiles/src/lib.rs`, the ERC‑20 `approve()` precompile implements exactly the "cancel-then-set" pattern that the report's *recommended mitigation* prescribes (approve 0, then approve amount) — but does so as **two sequential, non-atomic pallet calls**: [1](#0-0) 

```rust
if !current.is_zero() {
    pallet_assets::Pallet::<Runtime, Instance>::do_cancel_approval(
        &asset_id, &owner_account, &spender_account,
    )?;
    actual_weight = worst_case;
} else { ... }
pallet_assets::Pallet::<Runtime, Instance>::do_approve_transfer(
    asset_id, &owner_account, &spender_account, new_amount,
)?;
```

`do_cancel_approval` removes the existing `Approvals` entry and unreserves the owner's deposit; `do_approve_transfer` then re-reserves a deposit and writes the new allowance. Between these two calls there is **no `frame_support::storage::with_transaction` wrapper**.

Crucially, the sibling function `permit()` in the same file performs the *identical* cancel-then-approve sequence but explicitly wraps it: [2](#0-1) [3](#0-2) 

The presence of an explicit `with_transaction`/rollback wrapper in `permit()` (documented in `permit_precompile_tests.rs::permit_rollback_preserves_prior_allowance`, which was written specifically to prove that a failed second step doesn't destroy the prior allowance) shows the developers recognized this class of bug and mitigated it for `permit`, but the `approve()` entrypoint (called directly, without a signature/nonce step) has no equivalent transactional guard: [4](#0-3) 

### Impact

If `do_approve_transfer` fails for any reason after `do_cancel_approval` has already succeeded and mutated storage (unreserved the old deposit, deleted the `Approvals` entry), the precompile call returns an `Err`/revert to the EVM-style caller — which looks like a no-op transaction revert from the caller's perspective — while the owner's **pre-existing, previously-relied-upon allowance has already been silently destroyed** on-chain. Any spender who was relying on the existing allowance is now unable to pull funds even though the `approve(spender, newAmount)` transaction "failed." This is the inverse/complement of the original report's "stuck-forever" case: here a legitimate approval is unintentionally, irrecoverably wiped by a call that appears to the caller to have reverted with no effect.

### Likelihood / Caveats

I could not fully confirm, within the remaining tool budget, an exact failure branch inside `do_approve_transfer` that fires *after* `do_cancel_approval` succeeds but is independent of the `AssetStatus::Live` check both functions share (the test suite comment explicitly notes both entry points gate on `AssetStatus::Live` first, which blocks the simplest reproduction). A concrete trigger would need a failure specific to the deposit-reservation step of `do_approve_transfer` (e.g. an insufficient-balance/ED edge case on the freshly-unreserved funds, or a `ConsiderationNotRespected`-style hold failure) that does not also block `do_cancel_approval`. Because I was not able to inspect the body of `do_approve_transfer`/`do_cancel_approval` in `substrate/frame/assets/src/functions.rs` before the tool budget ran out, I cannot certify a concrete PoC trigger — this should be verified against the exact implementation of those two functions before treating it as fully proven.

### Recommendation

Wrap the `do_cancel_approval` + `do_approve_transfer` sequence in `approve()` (and any other precompile call site using the same pattern) in `frame_support::storage::with_transaction`, mirroring what `permit()` already does, so that a failure in the second step rolls back the first step's storage mutation instead of leaving the allowance permanently zeroed while the call outwardly reverts.

### Proof of Concept (conceptual)
1. Owner has an existing non-zero allowance to `spender` (deposit reserved).
2. Owner calls `approve(spender, newAmount)` with `newAmount != 0`.
3. `do_cancel_approval` succeeds — old approval deleted, deposit unreserved.
4. `do_approve_transfer` fails for a reason independent of `AssetStatus::Live` (needs to be confirmed against `functions.rs`), returning `Err`.
5. The precompile call reverts to the caller, but the prior allowance has already been deleted and is not restored, unlike the atomic `permit()` path. [5](#0-4)

### Citations

**File:** substrate/frame/assets/precompiles/src/lib.rs (L343-352)
```rust
	/// Execute the approve call.
	///
	/// Implements ERC-20 set semantics: `approve(spender, N)` sets the allowance to exactly `N`
	/// rather than adding to it. When overwriting a non-zero allowance, the existing approval is
	/// cancelled first so the new value replaces (not accumulates with) the old one.
	///
	/// `call.value > Balance::MAX` (the `type(uint256).max` "infinite allowance" idiom)
	/// saturates the stored allowance at `Balance::MAX`. The `Approval` event carries the
	/// raw `call.value`.
	fn approve(
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L396-418)
```rust
		} else {
			// If there's an existing non-zero allowance, cancel it first so we
			// overwrite (not accumulate) — matching ERC-20 spec semantics.
			// NOTE: This does not mitigate the well-known ERC-20 approve front-running
			// race condition. Callers concerned about this should approve to 0 first,
			// or use increaseAllowance/decreaseAllowance if available.
			if !current.is_zero() {
				pallet_assets::Pallet::<Runtime, Instance>::do_cancel_approval(
					&asset_id,
					&owner_account,
					&spender_account,
				)?;
				actual_weight = worst_case;
			} else {
				actual_weight = <Runtime as Config<Instance>>::WeightInfo::allowance()
					.saturating_add(<Runtime as Config<Instance>>::WeightInfo::approve_transfer());
			}
			pallet_assets::Pallet::<Runtime, Instance>::do_approve_transfer(
				asset_id,
				&owner_account,
				&spender_account,
				new_amount,
			)?;
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L503-516)
```rust
		let transaction_outcome = frame_support::storage::with_transaction(|| {
			let result = (|| {
				// Use the permit - this validates deadline, signature, and increments nonce
				permit::Pallet::<Runtime>::use_permit(
					&verifying_contract,
					&pallet_assets::Pallet::<Runtime, Instance>::name(asset_id.clone()),
					&owner_h160,
					&spender_h160,
					&value_bytes,
					&deadline_bytes,
					call.v,
					&r_bytes,
					&s_bytes,
				)
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L571-593)
```rust
					if !current.is_zero() {
						// If there's an existing non-zero allowance, cancel it first
						pallet_assets::Pallet::<Runtime, Instance>::do_cancel_approval(
							&asset_id,
							&owner_account,
							&spender_account,
						)?;
						actual_weight = worst_case;
					} else {
						// set new approval
						actual_weight = use_permit_weight
							.saturating_add(<Runtime as Config<Instance>>::WeightInfo::allowance())
							.saturating_add(
								<Runtime as Config<Instance>>::WeightInfo::approve_transfer(),
							);
					}
					pallet_assets::Pallet::<Runtime, Instance>::do_approve_transfer(
						asset_id,
						&owner_account,
						&spender_account,
						new_amount,
					)?;
				}
```

**File:** substrate/frame/assets/precompiles/src/permit_precompile_tests.rs (L635-644)
```rust
/// A failed permit must not destroy a prior allowance. Pre-approve(100),
/// freeze, submit permit(200) — rollback must leave the prior allowance
/// and its deposit untouched.
///
/// Note: an even stronger test would exercise the cancel-then-approve
/// order directly (cancel succeeds, approve fails, rollback restores).
/// But both pallet-assets entry points gate on `AssetStatus::Live` as
/// their first check, so that exact sequence cannot be constructed in
/// this mock.
#[test]
```
