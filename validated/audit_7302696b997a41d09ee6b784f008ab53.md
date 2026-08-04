## Finding a local analog

The Solana report's core broken invariant is: **a party under the counterparty's indirect control (the ATA's ownership) can be changed after a lock is created, so that a required payout transfer fails — yet the protocol still tears down the escrow state as if the payout succeeded, permanently stranding the funds.** The closest exact analog in this repository is in `pallet-child-bounties`.

### Title
Permanent fund lock in `claim_child_bounty`: payout transfer failures are ignored and bounty state is unconditionally destroyed - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`claim_child_bounty` moves funds out of the `child_bounty_account_id` to the curator and beneficiary using `T::Currency::transfer(..., AllowDeath)`. The transfer results are only checked with `debug_assert!`, which is a no-op in release/production builds. Regardless of whether the transfers actually succeeded, the extrinsic unconditionally removes the `ChildBounties` storage entry (`*maybe_child_bounty = None;`) and decrements bookkeeping counters. If the payout transfer fails (e.g. because the beneficiary account does not exist and the payout amount is below the existential deposit, producing `TokenError::CannotCreate`, or the account is otherwise unable to receive funds), the child bounty record disappears from storage but the funds remain in `child_bounty_account_id` — an account nobody can reference again, since there is no reclaim mechanism analogous to `pallet-bounties::reclaim_bounty_funds` (added in PR #11045 / prdoc `pr_10729`) for child bounties.

### Finding Description [1](#0-0) 
The relevant code:
```rust
let fee_transfer_result = T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath);
debug_assert!(fee_transfer_result.is_ok());

let payout_transfer_result = T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath);
debug_assert!(payout_transfer_result.is_ok());
```
Neither `fee_transfer_result` nor `payout_transfer_result` is propagated with `?`; both are discarded except for a `debug_assert!` that compiles to nothing outside test/debug builds. Immediately after, at [2](#0-1)  the code unconditionally decrements `ParentChildBounties`, removes `ChildBountyDescriptionsV1`, and sets `*maybe_child_bounty = None`, deleting all trace of the child bounty and its escrow account from state — independent of whether the transfer above actually moved the funds.

This mirrors the reported Solana bug class: the beneficiary account's ability to receive funds (its "ownership"/existence state) is not verified before the protocol commits to settlement and destroys the escrow record. Once the `ChildBounties` entry is gone, `close_child_bounty`, `award_child_bounty`, and `claim_child_bounty` all fail with `InvalidIndex` for that `(parent_bounty_id, child_bounty_id)` pair, so the stranded balance in `child_bounty_account_id` can never be swept — there is no `reclaim_child_bounty_funds` equivalent to what was added for the parent `pallet-bounties` pallet in the same codebase (`substrate/frame/bounties/src/lib.rs`, prdoc `pr_11045.prdoc`, `pr_10729.prdoc`).

Existing guards do not stop this because:
- `claim_child_bounty` is permissionless (`ensure_signed(origin)` only, no beneficiary check), so anyone can trigger the claim once `unlock_at` has passed.
- The only failure signal (`debug_assert!`) is stripped in production runtime builds (release builds used for actual chains do not enable `debug-assertions` by default).
- State cleanup and value transfer are not executed atomically/conditionally — cleanup proceeds even when the transfer fails, violating the required invariant that "payout state must only advance after ... settlement succeeds atomically."

### Impact Explanation
This is a permanent user-fund lock: bounty value intended for a beneficiary (and/or curator fee) can become permanently unrecoverable, with no admin, governance, or permissionless path to reclaim it, once the account backing the escrow is orphaned in storage. This falls squarely within the Impact Gate category "permanent user-fund or bridge-state lock" and does not require a malicious validator, collator, relayer, or governance actor — only an ordinary beneficiary account that is unfunded/non-existent (which can trivially happen if the beneficiary chosen at `award_child_bounty` time has never received an existential deposit, or has since been reaped), combined with any unprivileged account calling `claim_child_bounty`.

### Likelihood Explanation
Likelihood is moderate-to-high: `award_child_bounty` allows the curator to specify an arbitrary `beneficiary: AccountIdLookupOf<T>` with no requirement that the account exists or holds the existential deposit (see [3](#0-2) ). If `payout` (bounty value minus curator fee) ends up below the chain's existential deposit and the beneficiary account was never created, `Currency::transfer(..., AllowDeath)` returns `TokenError::CannotCreate`. Any signed account can then call `claim_child_bounty` once `unlock_at` passes, silently locking the funds forever. This can occur accidentally (misconfigured beneficiary/fee split) or be induced deliberately by whoever controls the beneficiary account.

### Recommendation
- Propagate the transfer results with `?` instead of `debug_assert!`, and only clear/mutate the `ChildBounties`, `ParentChildBounties`, and `ChildBountyDescriptionsV1` storage after both transfers succeed (or handle partial failure explicitly, e.g. leave the record in a recoverable "payout failed" state).
- Alternatively/additionally, add a permissionless `reclaim_child_bounty_funds` extrinsic mirroring `pallet_bounties::reclaim_bounty_funds`, so any stranded balance left in a closed/removed child bounty account can be swept back to the treasury or retried.
- Validate that the `beneficiary` account can actually receive the intended payout (e.g., is at least the existential deposit, or perform a `keep_alive`/provider check) at `award_child_bounty` time rather than deferring the failure to `claim_child_bounty`.

### Proof of Concept
1. Governance/curator creates a parent bounty and a child bounty with a small `value` (e.g., only slightly above ED) and a nonzero curator `fee` such that `payout = value - fee` is below the existential deposit.
2. Curator calls `award_child_bounty` with `beneficiary` set to a fresh account that has never held a balance (does not exist in `System::Account`).
3. After `unlock_at`, any signed account calls `claim_child_bounty(parent_bounty_id, child_bounty_id)`.
4. `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` fails with `TokenError::CannotCreate` since `payout < ExistentialDeposit` and the destination doesn't exist; `payout_transfer_result` is `Err(_)`, but `debug_assert!` is compiled out in the release runtime, so nothing stops execution.
5. The child bounty entry is removed from `ChildBounties`, `ParentChildBounties` counter decremented, and `Claimed` event is emitted claiming a successful payout that never happened.
6. The `payout` amount remains permanently locked in `child_bounty_account_id`; no dispatchable call can reference `(parent_bounty_id, child_bounty_id)` again to recover it, since it no longer exists in storage.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L619-650)
```rust
		pub fn award_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
			beneficiary: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

			// Ensure parent bounty exists, and is active.
			let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;

			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					// Ensure child-bounty is in active state.
					if let ChildBountyStatus::Active { ref curator } = child_bounty.status {
						ensure!(
							signer == *curator || signer == parent_curator,
							BountiesError::<T>::RequireCurator,
						);
						// Move the child-bounty state to pending payout.
						child_bounty.status = ChildBountyStatus::PendingPayout {
							curator: signer,
							beneficiary: beneficiary.clone(),
							unlock_at: Self::treasury_block_number() +
								T::BountyDepositPayoutDelay::get(),
						};
```

**File:** substrate/frame/child-bounties/src/lib.rs (L714-744)
```rust
						// Make curator fee payment.
						let child_bounty_account =
							Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
						let balance = T::Currency::free_balance(&child_bounty_account);
						let curator_fee = child_bounty.fee.min(balance);
						let payout = balance.saturating_sub(curator_fee);

						// Unreserve the curator deposit. Should not fail
						// because the deposit is always reserved when curator is
						// assigned.
						let _ = T::Currency::unreserve(curator, child_bounty.curator_deposit);

						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());
```

**File:** substrate/frame/child-bounties/src/lib.rs (L754-763)
```rust
						// Update the active child-bounty tracking count.
						ParentChildBounties::<T>::mutate(parent_bounty_id, |count| {
							count.saturating_dec()
						});

						// Remove the child-bounty description.
						ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

						// Remove the child-bounty instance from the state.
						*maybe_child_bounty = None;
```
