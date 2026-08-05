### Title
Silent payout failure via `debug_assert!` in `claim_child_bounty` permanently locks child-bounty funds - ([File: substrate/frame/child-bounties/src/lib.rs])

### Summary
`pallet-child-bounties::claim_child_bounty` performs two `T::Currency::transfer` calls (curator fee and beneficiary payout) and only checks their success with `debug_assert!`, which is compiled out in release/production builds. If either transfer fails, the function nevertheless proceeds to remove the child-bounty from storage. This mirrors the Augur `disavowCrowdsourcers` bug-class: a fund-moving step that can fail (there, via a malicious ERC777 hook; here, via ordinary `ExistentialDeposit`/`AllowDeath` semantics on the destination account) is not gated before the pallet irreversibly advances state, leading to permanently stranded funds with no code path left to reclaim them.

### Finding Description
In `claim_child_bounty`: [1](#0-0) 
the fee transfer to `curator` and payout transfer to `beneficiary` are performed with `T::Currency::transfer(..., AllowDeath)`, and their `Result`s are checked only via `debug_assert!(fee_transfer_result.is_ok())` / `debug_assert!(payout_transfer_result.is_ok())`. In a release build these assertions are no-ops, so a failing transfer is silently ignored. Regardless of outcome, the code proceeds to: [2](#0-1) 
decrement the active-child-bounty counter, remove the description, and set `*maybe_child_bounty = None` — permanently deleting the only on-chain reference that ties the derived `child_bounty_account` to any future payout logic.

`beneficiary` is attacker/curator-controlled (set earlier via the award step, reflected in `ChildBountyStatus::PendingPayout { curator, beneficiary, unlock_at }`), and the child-bounty `value`/`fee` are not bound to any enforced minimum beyond `fee < value` in `propose_curator`: [3](#0-2) 
A `payout` (`value - fee`) below the chain's `ExistentialDeposit`, sent with `AllowDeath` to a `beneficiary` account that does not already exist, causes `Currency::transfer` to return an error rather than creating a sub-ED account. The same applies to the fee transfer to `curator` if the fee is set to a small non-zero value and the curator account is not otherwise funded. Because the check is a `debug_assert!`, this failure is invisible in a compiled runtime, and the storage entry is deleted anyway — the remaining balance sitting in `child_bounty_account` (a deterministic, derived `AccountId` with no further pallet entry point referencing it) becomes permanently unreachable.

This directly parallels the reported invariant break: a value-transfer that can legitimately fail is not used as a gate for irreversible state transition, and once the state advances, there is no recovery path — funds are frozen exactly like the Augur dispute bonds.

### Impact Explanation
Any successful call to `claim_child_bounty` after this failure permanently strands the child bounty's remaining treasury-derived funds in an account nobody can ever spend from again (no owner, no further dispatchable references the account). This is a "permanent user-fund lock," one of the explicitly accepted impact categories, achieved without any privileged/governance action — the curator and the caller of `claim_child_bounty` are ordinary signed accounts, not admins.

### Likelihood Explanation
Triggering this only requires: (1) a child bounty whose `fee` or `value - fee` remainder is set below the runtime's `ExistentialDeposit`, and (2) a `curator`/`beneficiary` account that does not already hold a balance ≥ ED. Both are attacker-controllable through the ordinary `propose_curator` → `accept_curator` → award → `claim_child_bounty` flow, with no additional privilege needed. The bug is latent (only manifests in release builds where `debug_assert!` is compiled away), which is exactly the deployed configuration of production chains, making it realistically reachable rather than a purely theoretical debug-only issue.

### Recommendation
Replace `debug_assert!(fee_transfer_result.is_ok())` / `debug_assert!(payout_transfer_result.is_ok())` with proper `?`-propagated error handling (or equivalent `ensure!`) so that a failed transfer aborts the whole `try_mutate_exists` closure and rolls back the storage removal atomically with the transfer. Additionally, enforce a minimum child-bounty `fee` and payout remainder (or use `Preservation`/`KeepAlive` semantics with corrected handling) so that transfers to freshly-specified curator/beneficiary accounts cannot silently fail due to ED requirements.

### Proof of Concept
1. Governance/parent curator creates a parent bounty and calls `add_child_bounty` with a small `value` (e.g. slightly above ED).
2. `propose_curator` sets a nonzero `fee` for a `curator` account with zero prior balance, chosen such that `fee` (or `value - fee`) is below the chain's `ExistentialDeposit`.
3. `accept_curator` is called by that curator, reserving `curator_deposit`.
4. The bounty is awarded (`award_child_bounty`) to a fresh `beneficiary` account with zero balance.
5. Anyone calls `claim_child_bounty` after `unlock_at`. `T::Currency::transfer` to `curator` or `beneficiary` fails (`ExistentialDeposit`/`AllowDeath` violation on the destination), the `debug_assert!` is a no-op in release, and the function still deletes `ChildBounties` entry and `ChildBountyDescriptionsV1`.
6. The remaining balance in `child_bounty_account` is now permanently unreachable — no pallet call ever references that derived account again.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L383-384)
```rust
					// Ensure child-bounty curator fee is less than child-bounty value.
					ensure!(fee < child_bounty.value, BountiesError::<T>::InvalidFee);
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

**File:** substrate/frame/child-bounties/src/lib.rs (L754-765)
```rust
						// Update the active child-bounty tracking count.
						ParentChildBounties::<T>::mutate(parent_bounty_id, |count| {
							count.saturating_dec()
						});

						// Remove the child-bounty description.
						ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

						// Remove the child-bounty instance from the state.
						*maybe_child_bounty = None;

						Ok(())
```
