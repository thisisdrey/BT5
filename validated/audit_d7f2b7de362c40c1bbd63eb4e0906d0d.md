## Finding: `set_claim_permission` weight annotation is hardcoded and diverges from the pallet's own benchmarked `WeightInfo`, underpricing the actual DB work

### Title
`set_claim_permission` charges a fixed `reads_writes(1,1)` DbWeight instead of the benchmarked `WeightInfo::set_claim_permission()`, underpricing real storage access - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The dispatchable `set_claim_permission` does not use the pallet's generated `T::WeightInfo::set_claim_permission()` weight function like every other call in the pallet. Instead its `#[pallet::weight]` attribute is hardcoded to `T::DbWeight::get().reads_writes(1, 1)`, while the actual execution path performs additional storage reads via `Self::api_member_needs_delegate_migration(who)` that are not reflected in that fixed annotation.

### Finding Description
The call body is: [1](#0-0) 

```
#[pallet::call_index(15)]
#[pallet::weight(T::DbWeight::get().reads_writes(1, 1))]
pub fn set_claim_permission(...) -> DispatchResult {
    let who = ensure_signed(origin)?;
    ensure!(PoolMembers::<T>::contains_key(&who), Error::<T>::PoolMemberNotFound);
    ensure!(!Self::api_member_needs_delegate_migration(who.clone()), Error::<T>::NotMigrated);
    ClaimPermissions::<T>::mutate(who.clone(), |source| { *source = permission; });
    ...
}
```

`api_member_needs_delegate_migration` reads `PoolMembers`, then (when the pool's stake strategy is `Delegate`) calls `api_pool_needs_delegate_migration` (a `BondedPools` read plus a `StakeAdapter::pool_strategy` call) and `StakeAdapter::member_delegation_balance` (reads staking-side storage): [2](#0-1) 

The pallet's own benchmark for this exact extrinsic captures this extra cost. In the generic `weights.rs` it is modeled as 2 reads/1 write: [3](#0-2) 

But in a runtime using the `DelegateStake` adapter (e.g. Asset Hub Westend), the benchmarked cost of `set_claim_permission` is far higher, spanning `PoolMembers`, `BondedPools`, `System::Account`, `Staking::VirtualStakers`, `Staking::Bonded`, `Staking::Ledger`, `SubPoolsStorage`, `DelegatedStaking::Delegators`, and `ClaimPermissions` — 9 storage reads total: [4](#0-3) 

Every other similar call in the pallet (e.g. `bond_extra_other`) properly routes through `T::WeightInfo`: [5](#0-4) 

but `set_claim_permission` alone bypasses `T::WeightInfo::set_claim_permission()` and uses a fixed, config-independent `reads_writes(1, 1)`, meaning the weight charged at dispatch time never reflects the true, benchmarked, adapter-dependent cost.

Importantly, none of this reachable code iterates over the full `BondedPools` or `PoolMembers` maps — all accesses are single-key lookups keyed by the caller's own account/pool id, so there is no unbounded-list amplification vector here (repetition, batching order, or "stale record" shaping does not increase the per-call cost beyond the fixed ~9-read ceiling).

### Impact Explanation
Because the annotated weight is a compile-time constant that does not track `T::WeightInfo`, it can silently diverge from the actual measured cost whenever the runtime's `StakeAdapter`/storage layout changes (as demonstrated by the discrepancy between the plain `weights.rs` figure and the Asset-Hub-Westend-specific benchmark). Any signed pool member can call `set_claim_permission` at the charged price while the runtime performs materially more reads (and proof-size) than accounted for. Filling a block with many such calls (from many distinct pool-member accounts) causes real per-block I/O/proof-size consumption to exceed what the block's weight-based admission control assumes, which is the kind of "public underpriced work that degrades block production" the review criteria calls out.

### Likelihood Explanation
The bug is deterministic and requires no special state shaping: any pool member (a normal, permissionless action — joining a pool costs only the minimum join bond) can invoke `set_claim_permission` repeatedly. It does not require griefing accumulation via unbounded lists, stale records, or specific batching order — the divergence exists on every single call already, so a moderately funded attacker controlling many joined pool-member accounts can exploit it directly.

### Recommendation
Change the `#[pallet::weight]` attribute on `set_claim_permission` to use `T::WeightInfo::set_claim_permission()` (as done for all other calls in this pallet, e.g. `bond_extra_other`, `claim_commission`, etc.), so the charged weight tracks the pallet's actual benchmarked, adapter-dependent cost rather than a fixed, potentially stale `reads_writes(1, 1)` value.

### Proof of Concept
1. Deploy/observe on a runtime configured with `StakeAdapter = DelegateStake` (e.g. Asset Hub Westend-style config).
2. Have an account join a nomination pool (permissionless `join` call), becoming a `PoolMembers` entry.
3. Call `set_claim_permission` from that account.
4. Compare the weight actually consumed by the extrinsic (traceable via node metrics/benchmark harness replaying the same storage layout) against the weight charged by the dispatch's fixed `T::DbWeight::get().reads_writes(1, 1)` annotation — the benchmarked `WeightInfo::set_claim_permission()` for this configuration reports 9 reads / 1 write and ~107µs execution time versus the hardcoded 1 read / 1 write charged at dispatch.
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L4287-4309)
```rust
	pub fn api_member_needs_delegate_migration(who: T::AccountId) -> bool {
		// if the `Delegate` strategy is not used in the pallet, then no migration required.
		if T::StakeAdapter::strategy_type() != adapter::StakeStrategyType::Delegate {
			return false;
		}

		PoolMembers::<T>::get(who.clone())
			.map(|pool_member| {
				if Self::api_pool_needs_delegate_migration(pool_member.pool_id) {
					// the pool needs to be migrated before members can be migrated.
					return false;
				}

				let member_balance = pool_member.total_balance();
				let delegated_balance =
					T::StakeAdapter::member_delegation_balance(Member::from(who.clone()));

				// if the member has no delegation but has some balance in the pool, then it needs
				// to be migrated.
				delegated_balance.is_none() && !member_balance.is_zero()
			})
			.unwrap_or_default()
	}
```

**File:** substrate/frame/nomination-pools/src/weights.rs (L638-650)
```rust
	/// Storage: `NominationPools::PoolMembers` (r:1 w:0)
	/// Proof: `NominationPools::PoolMembers` (`max_values`: None, `max_size`: Some(237), added: 2712, mode: `MaxEncodedLen`)
	/// Storage: `NominationPools::ClaimPermissions` (r:1 w:1)
	/// Proof: `NominationPools::ClaimPermissions` (`max_values`: None, `max_size`: Some(41), added: 2516, mode: `MaxEncodedLen`)
	fn set_claim_permission() -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `542`
		//  Estimated: `3702`
		// Minimum execution time: 14_667_000 picoseconds.
		Weight::from_parts(15_242_000, 3702)
			.saturating_add(T::DbWeight::get().reads(2_u64))
			.saturating_add(T::DbWeight::get().writes(1_u64))
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/weights/pallet_nomination_pools.rs (L759-786)
```rust
	/// Storage: `NominationPools::PoolMembers` (r:1 w:0)
	/// Proof: `NominationPools::PoolMembers` (`max_values`: None, `max_size`: Some(717), added: 3192, mode: `MaxEncodedLen`)
	/// Storage: `NominationPools::BondedPools` (r:1 w:0)
	/// Proof: `NominationPools::BondedPools` (`max_values`: None, `max_size`: Some(254), added: 2729, mode: `MaxEncodedLen`)
	/// Storage: `System::Account` (r:1 w:0)
	/// Proof: `System::Account` (`max_values`: None, `max_size`: Some(128), added: 2603, mode: `MaxEncodedLen`)
	/// Storage: `Staking::VirtualStakers` (r:1 w:0)
	/// Proof: `Staking::VirtualStakers` (`max_values`: None, `max_size`: Some(40), added: 2515, mode: `MaxEncodedLen`)
	/// Storage: `Staking::Bonded` (r:1 w:0)
	/// Proof: `Staking::Bonded` (`max_values`: None, `max_size`: Some(72), added: 2547, mode: `MaxEncodedLen`)
	/// Storage: `Staking::Ledger` (r:1 w:0)
	/// Proof: `Staking::Ledger` (`max_values`: None, `max_size`: Some(753), added: 3228, mode: `MaxEncodedLen`)
	/// Storage: `NominationPools::SubPoolsStorage` (r:1 w:0)
	/// Proof: `NominationPools::SubPoolsStorage` (`max_values`: None, `max_size`: Some(261), added: 2736, mode: `MaxEncodedLen`)
	/// Storage: `DelegatedStaking::Delegators` (r:1 w:0)
	/// Proof: `DelegatedStaking::Delegators` (`max_values`: None, `max_size`: Some(88), added: 2563, mode: `MaxEncodedLen`)
	/// Storage: `NominationPools::ClaimPermissions` (r:1 w:1)
	/// Proof: `NominationPools::ClaimPermissions` (`max_values`: None, `max_size`: Some(41), added: 2516, mode: `MaxEncodedLen`)
	fn set_claim_permission() -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `6427`
		//  Estimated: `4218`
		// Minimum execution time: 99_555_000 picoseconds.
		Weight::from_parts(107_112_000, 0)
			.saturating_add(Weight::from_parts(0, 4218))
			.saturating_add(T::DbWeight::get().reads(9))
			.saturating_add(T::DbWeight::get().writes(1))
	}
```
