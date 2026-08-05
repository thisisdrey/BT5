## Analysis

The Cantina report's core broken invariant: a public entrypoint lets an unprivileged caller **claim an identifier/slot as the "first owner"** using a caller-supplied value, and once claimed, all trust (fund routing, migration rights) is bound to that hijacked identity — with no way for the intended owner to reclaim it.

Searching `paritytech/polkadot-sdk` for the same primitive (permissionless registration into an identifier space that other unprivileged users/extrinsics later reference and trust) turns up `pallet_nomination_pools::create_with_pool_id`. [1](#0-0) 

```rust
#[pallet::call_index(7)]
#[pallet::weight(T::WeightInfo::create())]
pub fn create_with_pool_id(
    origin: OriginFor<T>,
    #[pallet::compact] amount: BalanceOf<T>,
    root: AccountIdLookupOf<T>,
    nominator: AccountIdLookupOf<T>,
    bouncer: AccountIdLookupOf<T>,
    pool_id: PoolId,
) -> DispatchResult {
    let depositor = ensure_signed(origin)?;

    ensure!(!BondedPools::<T>::contains_key(pool_id), Error::<T>::PoolIdInUse);
    ensure!(pool_id < LastPoolId::<T>::get(), Error::<T>::InvalidPoolId);

    Self::do_create(depositor, amount, root, nominator, bouncer, pool_id)
} [1](#0-0) 
```

This is directly analogous to `EarnStrategyRegistry.registerStrategy`, but confirmed as a *repository-local, non-frontrunning* variant of the same class: **identifier squatting** — the reuse of a previously-meaningful ID by an unrelated, unprivileged actor, which downstream users/extrinsics continue to trust by ID alone.

### Title
Nomination-pools `create_with_pool_id` allows squatting a previously-destroyed pool ID with attacker-controlled roles — ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`Pallet::create_with_pool_id` only checks that the target `pool_id` is not *currently* occupied (`!BondedPools::contains_key`) and is below `LastPoolId` — it performs no check that the ID was never previously used by a *different* pool with different roles. Any signed account can therefore reoccupy a pool ID that was legitimately created and later dissolved, becoming `depositor`/`root`/`nominator`/`bouncer` of the "new" pool at that same numeric ID.

### Finding Description
`BondedPools` entries are removed when a pool is fully dissolved via `dissolve_pool` (invoked from `withdraw_unbonded` once the depositor leaves), freeing the `pool_id` slot while `LastPoolId` remains unchanged. `create_with_pool_id` explicitly re-opens that freed slot to any caller: [2](#0-1) 

Unlike `create` (which always allocates the *next* fresh `pool_id`), `create_with_pool_id` is designed to let a caller pick an ID `< LastPoolId` that is currently free — with no binding to the original creator, no cooldown, and no distinguishing marker that the pool at that ID is a "new" incarnation. Any off-chain system, UI, other pallet integration, or a pending/future `join(pool_id, amount)` extrinsic that references pools purely by numeric `PoolId` (which is the pallet's only handle for a pool) cannot distinguish the original, vetted pool from the attacker's replacement occupying the same ID.

The `EarnStrategyRegistry` bug and this one share the identical broken invariant: **acceptance of a caller-supplied identity/role assignment into a shared identifier namespace without binding it to the entity that legitimately "owns" that slot.** In the Solidity case it was a race for a not-yet-registered ID; here it's a permanent race for a slot that was legitimately used and later freed, which is even less time-constrained than a mempool front-run, since the attacker can watch the chain and act at leisure once a pool fully dissolves, rather than a single transaction race.

### Impact Explanation
An attacker who identifies (or waits for) a dissolved pool ID can recreate a pool at that ID with themselves as `root`/`nominator`/`bouncer`. Any subsequent `join(pool_id, amount)` call submitted by users who believed they were joining the original, trusted pool (e.g., because their wallet/dApp cached the pool ID, or a delayed/queued transaction still targets that ID) will bond funds into the attacker-controlled pool instead. The attacker, as `root`, can then set 100% commission, block `nominate`, or otherwise divert/withhold delegated stake and rewards — fund loss for the deceived depositor/joiners, matching the "theft/unbacked mint or unlock" and "wrong beneficiary" categories in the impact gate.

### Likelihood Explanation
No privileged actor, governance, relayer, or malicious validator is required — any signed account with the minimal deposit can call `create_with_pool_id` as soon as a `pool_id` becomes free. Because pools are user-created and user-destroyed continuously in production (dissolution is a normal lifecycle event, not an edge case), free IDs recur naturally, giving an attacker an ongoing, low-cost opportunity window rather than a single fleeting mempool race — making this a persistent-scan attack rather than a one-shot front-run.

### Recommendation
Do not allow ID reuse at all, or if `create_with_pool_id` must exist for migration/benchmarking purposes, restrict it to a privileged origin, or bind the new pool at a reused ID to a fresh, unguessable, non-recycled identifier space (e.g., never allow `pool_id` values that were ever previously allocated, tracked via a monotonic "used" set rather than just `< LastPoolId` + "not currently bonded"). At minimum, gate this extrinsic behind a permissioned origin so it cannot be triggered by an arbitrary signed account.

### Proof of Concept
1. Alice creates a pool via `create`, receiving `pool_id = N`; users are told to `join(N, amount)`.
2. Alice's pool is fully dissolved (all members exit, depositor leaves) — `BondedPools::<T>::get(N)` is now `None`, but `LastPoolId::<T>::get() > N`. [3](#0-2)  — this exact resurrection sequence (`set_state(Destroying)` → fully unbond → `withdraw_unbonded` → `create_with_pool_id(..., 1)`) is demonstrated to succeed in the pallet's own test suite, confirming the mechanic is live and unguarded.
3. Mallory (any signed account, no special privilege) calls `create_with_pool_id(amount, root=Mallory, nominator=Mallory, bouncer=Mallory, pool_id=N)`; this passes both `PoolIdInUse` and `InvalidPoolId` checks and succeeds.
4. Any user who later calls `join(N, amount)` — believing pool `N` is still Alice's — bonds funds into Mallory's pool instead, where Mallory as `root` can set maximal commission or block withdrawal paths, misappropriating the deposited funds.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2596-2619)
```rust

		/// Create a new delegation pool with a previously used pool id
		///
		/// # Arguments
		///
		/// same as `create` with the inclusion of
		/// * `pool_id` - `A valid PoolId.
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::create())]
		pub fn create_with_pool_id(
			origin: OriginFor<T>,
			#[pallet::compact] amount: BalanceOf<T>,
			root: AccountIdLookupOf<T>,
			nominator: AccountIdLookupOf<T>,
			bouncer: AccountIdLookupOf<T>,
			pool_id: PoolId,
		) -> DispatchResult {
			let depositor = ensure_signed(origin)?;

			ensure!(!BondedPools::<T>::contains_key(pool_id), Error::<T>::PoolIdInUse);
			ensure!(pool_id < LastPoolId::<T>::get(), Error::<T>::InvalidPoolId);

			Self::do_create(depositor, amount, root, nominator, bouncer, pool_id)
		}
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L5042-5051)
```rust
			// start dismantling the pool.
			assert_ok!(Pools::set_state(RuntimeOrigin::signed(902), 1, PoolState::Destroying));
			assert_ok!(fully_unbond_permissioned(10));

			CurrentEra::set(3);
			assert_ok!(Pools::withdraw_unbonded(RuntimeOrigin::signed(10), 10, 10));

			assert_ok!(Pools::create_with_pool_id(RuntimeOrigin::signed(10), 20, 234, 654, 783, 1));
		});
	}
```
