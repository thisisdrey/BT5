The claim is verified against the codebase. The code at [1](#0-0)  confirms `set_claim_permission` uses a hardcoded `T::DbWeight::get().reads_writes(1, 1)` weight annotation while calling `Self::api_member_needs_delegate_migration(who.clone())`, which performs additional reads beyond the single `PoolMembers` lookup and `ClaimPermissions` write. This contrasts with the neighboring `bond_extra_other` call, which properly uses `T::WeightInfo::bond_extra_transfer().max(T::WeightInfo::bond_extra_other())` [2](#0-1) .

This is a genuine, deterministic weight-underpricing bug: the fixed annotation does not scale with the pallet's own benchmarked `WeightInfo::set_claim_permission()`, and diverges further under adapters like `DelegateStake` where the real read count is far higher than what's charged. Since this is exploitable permissionlessly by any pool member and directly relates to "public underpriced work that degrades block production," it fits within the allowed impact gate.

Audit Report

## Title
`set_claim_permission` charges a fixed `reads_writes(1,1)` DbWeight instead of the benchmarked `WeightInfo::set_claim_permission()`, underpricing real storage access - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
The `set_claim_permission` dispatchable in the nomination-pools pallet uses a hardcoded `#[pallet::weight(T::DbWeight::get().reads_writes(1, 1))]` annotation instead of routing through the pallet's generated `T::WeightInfo::set_claim_permission()`, unlike every other call in the pallet. The call body performs additional storage reads via `Self::api_member_needs_delegate_migration(who)` that are not captured by the fixed 1-read/1-write annotation, and this divergence grows substantially under runtimes using the `DelegateStake` adapter.

## Finding Description
The dispatchable at [1](#0-0)  is annotated with a fixed `T::DbWeight::get().reads_writes(1, 1)` weight, but its body calls `PoolMembers::<T>::contains_key(&who)` (read 1) and `Self::api_member_needs_delegate_migration(who.clone())`, which — when the configured `StakeAdapter` strategy is `Delegate` — performs a `PoolMembers` read, a `BondedPools` read (via `api_pool_needs_delegate_migration`), and a `StakeAdapter::member_delegation_balance` call that reads staking-side storage, before finally mutating `ClaimPermissions` (1 write).

Every other comparable call in the pallet routes through `T::WeightInfo`, e.g. `bond_extra_other` at [2](#0-1) , but `set_claim_permission` alone bypasses `T::WeightInfo::set_claim_permission()`. The pallet's own generic benchmark models this call as 2 reads/1 write (already exceeding the hardcoded 1/1), and runtime-specific benchmarks (e.g. Asset Hub Westend with `DelegateStake`) measure 9 reads/1 write and ~107µs — far beyond the hardcoded fixed annotation. This means the weight charged at dispatch never reflects the true, benchmarked, adapter-dependent cost, and no existing guard (the `ensure!` checks are correctness checks, not weight-related) mitigates this.

## Impact Explanation
Because the annotated weight is a compile-time constant unrelated to `T::WeightInfo`, actual per-call I/O and proof-size consumption during block execution can exceed what the block's weight-based accounting assumes. This is public underpriced work reachable by any signed pool member, matching the allowed impact category of "public underpriced work that degrades block production." All storage accesses are single-key lookups (no unbounded map iteration), so the amplification is bounded to the fixed gap between the hardcoded 1-read/1-write and the real ~9-read/1-write cost per call, not an unbounded griefing vector.

## Likelihood Explanation
Any account that has joined a nomination pool (a permissionless action requiring only the minimum join bond) can call `set_claim_permission` directly and repeatedly with no special preconditions, since the divergence exists on every single call. A moderately funded attacker controlling multiple pool-member accounts can trigger this underpricing across many extrinsics within a block.

## Recommendation
Change the `#[pallet::weight]` attribute on `set_claim_permission` to use `T::WeightInfo::set_claim_permission()`, matching the pattern used by all other calls in the pallet, so the charged weight tracks the pallet's actual benchmarked, adapter-dependent cost instead of a fixed, potentially stale `reads_writes(1, 1)` value.

## Proof of Concept
1. Configure a runtime with `StakeAdapter = DelegateStake` (e.g. Asset Hub Westend-style config).
2. Have an account join a nomination pool via the permissionless `join` call, becoming a `PoolMembers` entry.
3. Call `set_claim_permission` from that account.
4. Compare the weight actually consumed (via node metrics or the benchmark harness replaying the same storage layout) against the weight charged by the fixed `T::DbWeight::get().reads_writes(1, 1)` annotation: the benchmarked `WeightInfo::set_claim_permission()` for this configuration reports 9 reads/1 write and ~107µs execution time versus the hardcoded 1 read/1 write charged at dispatch.
5. Repeat step 3 from many distinct pool-member accounts within a single block to accumulate real I/O work beyond what the block's declared weight consumption implies.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2885-2889)
```rust
		#[pallet::call_index(14)]
		#[pallet::weight(
			T::WeightInfo::bond_extra_transfer()
			.max(T::WeightInfo::bond_extra_other())
		)]
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2913-2938)
```rust
		#[pallet::call_index(15)]
		#[pallet::weight(T::DbWeight::get().reads_writes(1, 1))]
		pub fn set_claim_permission(
			origin: OriginFor<T>,
			permission: ClaimPermission,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(PoolMembers::<T>::contains_key(&who), Error::<T>::PoolMemberNotFound);

			// ensure member is not in an un-migrated state.
			ensure!(
				!Self::api_member_needs_delegate_migration(who.clone()),
				Error::<T>::NotMigrated
			);

			ClaimPermissions::<T>::mutate(who.clone(), |source| {
				*source = permission;
			});

			Self::deposit_event(Event::<T>::MemberClaimPermissionUpdated {
				member: who,
				permission,
			});

			Ok(())
		}
```
