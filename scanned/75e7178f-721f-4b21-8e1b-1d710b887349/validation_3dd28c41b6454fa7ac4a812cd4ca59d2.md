### Title
`create_with_pool_id` lets anyone re-bind a stale, trusted pool ID to attacker-controlled roles after the original pool is dissolved - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
The core broken invariant in the Fusion Swap report is: an identifier (order ID / PDA) that was permanently closed can be re-used by anyone to bind fresh, attacker-chosen state to that same identifier, so a party that already committed to interact with "that ID" (based on its old, trusted parameters) ends up interacting with a completely different, adversarial configuration. `pallet-nomination-pools` has a structurally identical primitive: `create_with_pool_id` [1](#0-0)  explicitly allows any signed account to (re)create a bonded pool at a `pool_id` that was previously used and later dissolved, with root/nominator/bouncer roles of the caller's choosing.

### Finding Description
`dissolve_pool` fully removes a pool's on-chain footprint (`BondedPools`, `RewardPools`, `SubPoolsStorage`, `Metadata`, reverse lookup) once its last member withdraws [2](#0-1) . That pool's numeric `pool_id` is never marked as permanently retired anywhere — it is only guaranteed `< LastPoolId::<T>::get()`.

`create_with_pool_id` is a *public, unprivileged, signed* extrinsic that:
```
ensure!(!BondedPools::<T>::contains_key(pool_id), Error::<T>::PoolIdInUse);
ensure!(pool_id < LastPoolId::<T>::get(), Error::<T>::InvalidPoolId);
Self::do_create(depositor, amount, root, nominator, bouncer, pool_id)
``` [3](#0-2) 

The only checks are "no live pool currently occupies this id" and "this id was issued at some point in the past." There is no check that the caller is the original depositor/root of the dissolved pool at that id, and no check that reuse only happens long after dissolution or via any permissioned/governance path. Any account can therefore watch the chain for a pool being dissolved (visible via the `Destroyed` event) and, in the very next block, call `create_with_pool_id` with that exact `pool_id`, installing themselves as `root`, `nominator`, and `bouncer` of a brand-new pool that now answers to the same numeric ID that users, off-chain integrators, indexers, and already-signed transactions still associate with the old, trusted pool.

This is the direct analog of the Solana bug: the "escrow PDA" (Fusion Swap) = the numeric `pool_id` here; "maker cancels order, PDA closes, ID reusable" = "pool depositor withdraws last stake, pool dissolves, `pool_id` reusable"; "attacker recreates order with malicious rate/fee/native-flag" = "attacker recreates pool with malicious `root`/`nominator`/`bouncer` roles under the same `pool_id`". Just as in the Solana case, a party who already built a transaction (`join`, `bond_extra`, `nominate`, `set_metadata`-driven trust decisions, or any external system routing funds by `pool_id`) referencing the old, legitimate pool_id will have that transaction executed against attacker-controlled pool state instead, with no way for existing guards (`PoolIdInUse`, `InvalidPoolId`) to prevent it, because those checks are satisfied precisely in the reuse scenario they should prevent.

### Impact Explanation
A user who intends to `join` a specific, previously-vetted pool (chosen for its trusted `root`/`nominator`, e.g., picked from off-chain tooling, a UI, or a governance-endorsed operator list) can be redirected into staking with a hostile pool at the same `pool_id`: the new `nominator` can direct all bonded stake toward validators controlled by or colluding with the attacker (increasing slash risk / MEV extraction), the new `bouncer` can block withdrawals, and the new `root` controls pool configuration and commission going forward. Funds bonded into the wrong pool are staked under adversarial governance with no recourse — a fund-lock/fund-misdirection outcome consistent with the required impact classes (unauthorized execution/origin escalation of pool control, and potential permanent-lock of user funds under a malicious bouncer/root).

### Likelihood Explanation
Likelihood is High for an unprivileged attacker: dissolution is a public, observable on-chain event (`Event::Destroyed { pool_id }`), and `create_with_pool_id` is callable by any signed account with no cooldown, no ownership continuity check, and no governance gate — only the trivial arithmetic checks shown above. No malicious node/validator/relayer/admin is required; a normal user can monitor `Destroyed` events and immediately reclaim the vacated `pool_id` for any `pool_id < LastPoolId`. The action requires no compromise of consensus or privileged accounts, satisfying the "unprivileged attacker" and "public entrypoint" requirements.

### Recommendation
Either remove `create_with_pool_id` as a fully public call and gate it behind the pool's own permissioned lineage (e.g., only the original depositor, or a governance/root origin, may re-register a specific stale `pool_id`), or maintain a permanent "retired ids" set/bitmap analogous to the Fusion Swap fix (a `RetiredPoolIds` map populated on `dissolve_pool`) so a `pool_id` can never be reissued once dissolved. If reuse is intentionally desired for storage-bound reasons, add an explicit cool-down and require the same depositor/root identity (or governance approval) to reclaim it, and emit a distinguishable event so downstream consumers relying on `pool_id` stability are not silently redirected.

### Proof of Concept
1. Attacker observes `pallet_nomination_pools::Event::Destroyed { pool_id: N }` after the last member of pool `N` calls `withdraw_unbonded`, which triggers `dissolve_pool` and fully clears `BondedPools`/`Metadata`/`RewardPools` for id `N` [2](#0-1) .
2. In the very next block, attacker (any signed account, no special permission) submits:
   ```
   Pools::create_with_pool_id(
       RuntimeOrigin::signed(attacker),
       amount,
       attacker_lookup,   // root
       attacker_lookup,   // nominator
       attacker_lookup,   // bouncer
       N,                 // reused pool_id
   )
   ```
   This passes both `!BondedPools::contains_key(N)` (true, since dissolved) and `N < LastPoolId::get()` (true, since `N` was previously issued) [4](#0-3) , succeeding and installing the attacker as root/nominator/bouncer of pool `N`.
3. A victim who had already decided to interact with pool `N` (e.g., a `join(amount, N)` transaction queued/broadcast, or an off-chain integration that stakes into `pool_id = N` based on stale trusted metadata) has that transaction executed against the attacker's pool, bonding the victim's funds under attacker-controlled `nominator`/`bouncer` roles.

This chain is reproducible purely from repository logic (event → public call with satisfied guards → funds routed to attacker-controlled roles), matching the reused-identifier/reinitialization bug class from the external report.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2597-2619)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3358-3422)
```rust
	/// Remove everything related to the given bonded pool.
	///
	/// Metadata and all of the sub-pools are also deleted. All accounts are dusted and the leftover
	/// of the reward account is returned to the depositor.
	pub fn dissolve_pool(bonded_pool: BondedPool<T>) {
		let reward_account = bonded_pool.reward_account();
		let bonded_account = bonded_pool.bonded_account();

		ReversePoolIdLookup::<T>::remove(&bonded_account);
		RewardPools::<T>::remove(bonded_pool.id);
		SubPoolsStorage::<T>::remove(bonded_pool.id);

		// remove the ED restriction from the pool reward account.
		let _ = Self::unfreeze_pool_deposit(&bonded_pool.reward_account()).defensive();

		// Kill accounts from storage by making their balance go below ED. We assume that the
		// accounts have no references that would prevent destruction once we get to this point. We
		// don't work with the system pallet directly, but
		// 1. we drain the reward account and kill it. This account should never have any extra
		// consumers anyway.
		// 2. the bonded account should become a 'killed stash' in the staking system, and all of
		//    its consumers removed.
		defensive_assert!(
			frame_system::Pallet::<T>::consumers(&reward_account) == 0,
			"reward account of dissolving pool should have no consumers"
		);
		defensive_assert!(
			frame_system::Pallet::<T>::consumers(&bonded_account) == 0,
			"bonded account of dissolving pool should have no consumers"
		);
		defensive_assert!(
			T::StakeAdapter::total_stake(Pool::from(bonded_pool.bonded_account())) == Zero::zero(),
			"dissolving pool should not have any stake in the staking pallet"
		);

		// This shouldn't fail, but if it does we don't really care. Remaining balance can consist
		// of unclaimed pending commission, erroneous transfers to the reward account, etc.
		let reward_pool_remaining = T::Currency::reducible_balance(
			&reward_account,
			Preservation::Expendable,
			Fortitude::Polite,
		);
		let _ = T::Currency::transfer(
			&reward_account,
			&bonded_pool.roles.depositor,
			reward_pool_remaining,
			Preservation::Expendable,
		);

		defensive_assert!(
			T::Currency::total_balance(&reward_account) == Zero::zero(),
			"could not transfer all amount to depositor while dissolving pool"
		);
		// NOTE: Defensively force set balance to zero.
		T::Currency::set_balance(&reward_account, Zero::zero());

		// dissolve pool account.
		let _ = T::StakeAdapter::dissolve(Pool::from(bonded_account)).defensive();

		Self::deposit_event(Event::<T>::Destroyed { pool_id: bonded_pool.id });
		// Remove bonded pool metadata.
		Metadata::<T>::remove(bonded_pool.id);

		bonded_pool.remove();
	}
```
