### Title
Unchecked payout transfer results in `claim_child_bounty`/`claim_bounty` lead to permanent fund lock on silent transfer failure - (File: `substrate/frame/child-bounties/src/lib.rs`, `substrate/frame/bounties/src/lib.rs`)

### Summary
`claim_child_bounty` (and the analogous `claim_bounty`) perform the beneficiary/curator payout via `T::Currency::transfer(...)` and only verify success with `debug_assert!(...)` rather than propagating the `DispatchResult`. `debug_assert!` compiles to a no-op in release builds, which is how production Substrate-based chains run. This is a direct structural analog of the reported "unchecked transfer return value" bug class: the return value of an external value-moving call is not actually checked in production, so the pallet proceeds to finalize state (delete the bounty, emit `Claimed`) even if the underlying transfer silently failed.

### Finding Description
In `claim_child_bounty`: [1](#0-0) 

the curator-fee and beneficiary payouts are executed with `Preservation::AllowDeath`/`ExistenceRequirement::AllowDeath` and their `Result` is discarded via `debug_assert!(fee_transfer_result.is_ok())` / `debug_assert!(payout_transfer_result.is_ok())`. In a release-profile runtime (the configuration all production Substrate/Polkadot-SDK chains ship with), `debug_assert!` is a no-op — the check literally does not execute, so a failed transfer is never detected.

Immediately after these unchecked calls, the code unconditionally:
- Emits `Event::Claimed { index, child_index, payout, beneficiary }` claiming the payout occurred,
- Decrements `ParentChildBounties` count,
- Removes `ChildBountyDescriptionsV1`,
- Sets `*maybe_child_bounty = None`, permanently removing the only on-chain reference to `child_bounty_account_id(parent_bounty_id, child_bounty_id)`.

`AllowDeath` on the *sender* side does not guarantee the transfer will succeed: if `beneficiary` (or `curator`) does not yet exist as an account and `payout` (or `curator_fee`) is below `T::ExistentialDeposit`, the currency implementation will fail to create that destination account and return an `Err` (a new account cannot be created below the existential deposit regardless of the sender's preservation setting). The developer comments "should not fail" / "should not fail because curator fee is always less than bounty value" show this was an assumed invariant, not an enforced one — and it is not: dust amounts below ED to a not-yet-existing beneficiary are a realistic path.

Because the check that would have caught this (`debug_assert!`) is stripped in production, the pallet has no actual guard against this failure mode. The funds remain stranded in `child_bounty_account_id(...)`, but the storage entry that is the *only* mechanism to reference/re-claim that account is deleted in the same call. There is no other dispatchable that can recover funds from an already-removed child bounty account, since the account id is only ever derived from the (now-gone) bounty indices in normal flows.

The identical pattern exists in the parent pallet's `claim_bounty`: [2](#0-1) 

### Impact Explanation
This breaks the SDK-wide invariant that "balances, ... treasury spends, ... must conserve value and settle exactly once to the rightful beneficiary and amount" and "queues/... payout state must only advance after ... settlement succeed[s] atomically." Here settlement (the transfer) can fail while payout state (bounty removal + `Claimed` event) advances anyway, causing a **permanent, unrecoverable lock of user funds** — the treasury-derived value sitting in the child-bounty account becomes economically stranded with no code path left to move it, since its only handle (the `ChildBounties` storage entry) is deleted unconditionally in the same transaction. This is exactly the "permanent user-fund lock" impact category called out in the Impact Gate.

### Likelihood Explanation
The trigger condition is not exotic: it only requires a `beneficiary` (or `curator`) account that has never held a balance before, receiving a `payout` (or `curator_fee`) below `ExistentialDeposit`. Curators/proposers/callers control the bounty `value`/`fee` split at proposal/award time, and `claim_child_bounty` is callable by any signed account ("Call works independent of parent bounty state... The dispatch origin for this call may be any signed origin"). An attacker or even an ordinary user can engineer a dust-sized `curator_fee` or a dust `payout` remaining after rounding on a fresh account, satisfying the failure precondition without needing any privileged, admin, or off-chain relayer/validator involvement — matching the "unprivileged attacker" bar required by the gate.

### Recommendation
Replace `debug_assert!(transfer_result.is_ok())` with proper error propagation (`?`) so that a failed transfer aborts the whole `try_mutate_exists` closure and rolls back all associated state changes (event, counters, storage removal). Apply the same fix to the parallel pattern in `substrate/frame/bounties/src/lib.rs::claim_bounty`. Alternatively, use `Preservation::Expendable`/guaranteed-success currency primitives, or explicitly top up destination accounts to at least ED before transferring dust amounts, and only finalize/remove bounty storage after transfers are confirmed successful.

### Proof of Concept
1. Propose and approve a bounty/child-bounty such that, after curator assignment, `child_bounty.fee` is a small non-zero value (e.g., 1 unit) and the resulting `payout = balance - curator_fee` is likewise small.
2. Assign a `curator` and `beneficiary` that are brand-new accounts (never funded, not present in `System::Account`), and ensure `curator_fee` (or `payout`) is below the chain's `ExistentialDeposit`.
3. Progress the child bounty to `PendingPayout` and wait past `unlock_at`.
4. Call `claim_child_bounty` (any signed account) in a release-built runtime.
5. `T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath)` (or the beneficiary transfer) returns `Err` because the destination account cannot be created below ED; `debug_assert!` is compiled out and does nothing.
6. Execution continues: `Event::Claimed` fires, `ParentChildBounties` count decrements, `ChildBountyDescriptionsV1` is removed, and `*maybe_child_bounty = None`.
7. The funds remain in `child_bounty_account_id(parent_bounty_id, child_bounty_id)`, but there is no longer any `ChildBounties` entry referencing that account — the value is permanently stranded.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L726-744)
```rust
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

**File:** substrate/frame/bounties/src/lib.rs (L820-827)
```rust
					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());

```
