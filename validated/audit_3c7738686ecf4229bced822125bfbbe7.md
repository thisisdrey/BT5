## Analysis

The RocketPool bug's core invariant is: **a shared, per-protocol delay/queue resource, resettable by any depositor's routine action, can be kept permanently "full"/"unexpired" by a low-cost attacker, blocking a critical unstake/exit path for all other users of the same shared pool.**

The closest local analog in `paritytech/polkadot-sdk--018` is in `pallet-nomination-pools`, where **all members of a nomination pool share a single underlying staking ledger** (the pool's bonded/stash account), and that ledger's `unlocking` chunk queue is bounded by `T::MaxUnlockingChunks` at the staking-pallet level — a bound shared across every member, not per-member.

### Title
Shared bonded-account `unlocking` queue in `pallet-nomination-pools` lets a single low-stake member DoS all other members' `unbond` calls via `NoMoreChunks` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet_nomination_pools::Pallet::unbond` [1](#0-0)  forwards every member's unbond request to the *same* underlying staking stash — the pool's `bonded_account()` — via `T::StakeAdapter::unbond` [2](#0-1) . That call lands in the core staking pallet's `unbond`/`do_unbond`, which enforces a single bounded `unlocking: BoundedVec<UnlockChunk<...>, T::MaxUnlockingChunks>` queue on that one shared stash [3](#0-2) . Because this queue belongs to the pool's shared bonded account rather than to each individual member, any member can occupy queue slots that block *every other member's* unbond call.

### Finding Description
- The nomination-pools documentation itself states the risk plainly: *"If there are too many unlocking chunks to unbond with the pool account, [`Call::pool_withdraw_unbonded`] can be called... it may not be possible to release the current unlocking chunks, in which case, the result of this call will likely be the `NoMoreChunks` error"* [4](#0-3) .
- Each `unbond` extrinsic pushes/merges one `UnlockChunk{ value, era }` into the shared stash ledger, keyed by `active_era + bonding_duration`, with only one chunk per era but a hard cap of `MaxUnlockingChunks` (32 by default in `pallet-staking`, `MaxUnlockingChunks` in `pallet-staking-async`) simultaneous chunks [5](#0-4) .
- `do_unbond` only attempts an automatic `withdraw_unbonded` (freeing *matured* chunks, i.e., those whose era ≤ current active era) when the queue is *already full*; if it is full but no chunk has yet matured (because `BondingDuration` hasn't elapsed for any of them), the `ensure!(ledger.unlocking.len() < MaxUnlockingChunks, Error::NoMoreChunks)` check fails and the extrinsic reverts [6](#0-5) .
- A pool member is permitted to perform a *partial, permissioned* self-unbond as long as the remaining balance stays above `MinJoinBond`/`depositor_min_bond` [7](#0-6) . This lets an attacker split a modest stake into many small unbonds, one per era, each creating a fresh `UnlockChunk` on the pool's shared stash.
- By issuing one minimal unbond per era for `MaxUnlockingChunks` consecutive eras (rotating so a new chunk is always added before older ones mature/are cleared), the attacker keeps the shared queue permanently saturated with unmatured chunks. Every other pool member's `Pools::unbond` call then deterministically hits `pallet_staking::Error::NoMoreChunks`, exactly as demonstrated for the fully-saturated case in the integration test `automatic_unbonding_pools` [8](#0-7)  and in `unbonding_rejects_if_max_chunks` [9](#0-8) .

This is directly analogous to the RocketPool bug: a routine, minimal, repeatable action by one participant (staking small amounts / unbonding small amounts) resets/occupies a shared delay/queue resource that gates a critical exit path (`unstake`/`unbond`) for unrelated users.

### Impact Explanation
Any unprivileged account that can afford `MinJoinBond` (which can be set very low, e.g. existential deposit) can join a target nomination pool and, by issuing one cheap `unbond` extrinsic per era, indefinitely block all other members of that pool from exiting via `Call::unbond`. This is a fund-lock DoS on a core staking/exit primitive — members' capital remains bonded and inaccessible with no way to force an exit, since `pool_withdraw_unbonded` cannot free unmatured chunks either. This matches the accepted impact category "permanent user-fund... lock" from the gate criteria.

### Likelihood Explanation
The attack requires only: (1) joining a pool with minimal capital, (2) sending one low-value `unbond` extrinsic every era for as many eras as `MaxUnlockingChunks` allows, and (3) sustaining that cadence indefinitely. No validator, relayer, governance, or node compromise is needed — it is achievable by any signed account interacting with the public `Call::unbond` entrypoint. Larger, more actively used pools are more likely to already approach the `MaxUnlockingChunks` bound organically, lowering the attacker's required effort further, and smaller `MaxUnlockingChunks`/`MaxUnbonding` configurations (as shown configurable down to `1` in tests) make the attack trivial to sustain.

### Recommendation
Decouple the unlocking-chunk accounting from a single shared stash for pools with many members, e.g. by tracking per-member unbonding intents at the pool level (as `SubPools`/`unbonding_eras` already partially do) and batching/aggregating the underlying `StakeAdapter::unbond` calls so that a single member cannot monopolize the shared `MaxUnlockingChunks` slots. Alternatively, increase `MaxUnlockingChunks` for pool-adapter stashes proportionally to expected member counts, add a per-member rate limit on `Pools::unbond` frequency, or allow forced consolidation/withdrawal of matured chunks independent of queue fullness so genuine exits are never starved by an attacker's unmatured chunks.

### Proof of Concept
1. Attacker `A` (low balance ≥ `MinJoinBond`) joins pool `P` alongside victim `V`. Both share the same `bonded_account()` stash in `pallet-staking`.
2. For `MaxUnlockingChunks` consecutive eras, `A` calls `Pools::unbond(A, A, small_amount)` once per era (permissioned partial self-unbond, allowed by `ok_to_unbond_with`), each time creating a new `UnlockChunk{ era: current+BondingDuration }` on the pool's shared stash.
3. Once the shared ledger's `unlocking` vector reaches `MaxUnlockingChunks` and no chunk has yet matured (`era <= active_era`), `V` calls `Pools::unbond(V, V, amount)`.
4. `do_unbond` sees `unlocking.len() == MaxUnlockingChunks`, attempts `do_withdraw_unbonded`, finds no matured chunks to clear, and the subsequent `ensure!(... < MaxUnlockingChunks)` fails with `pallet_staking::Error::<T>::NoMoreChunks`, reproducing the exact failure path shown in `automatic_unbonding_pools` [10](#0-9)  and `unbonding_rejects_if_max_chunks` [11](#0-10) .
5. As long as `A` keeps injecting one fresh unmatured chunk per era faster than old ones mature, `V`'s exit remains permanently blocked.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1236-1252)
```rust
		// any partial unbonding is only ever allowed if this unbond is permissioned.
		ensure!(
			is_permissioned || is_full_unbond,
			Error::<T>::PartialUnbondNotAllowedPermissionlessly
		);

		// any unbond must comply with the balance condition:
		ensure!(
			is_full_unbond ||
				balance_after_unbond >=
					if is_depositor {
						Pallet::<T>::depositor_min_bond()
					} else {
						MinJoinBond::<T>::get()
					},
			Error::<T>::MinimumBondNotMet
		);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2246-2254)
```rust
		/// # Note
		///
		/// If there are too many unlocking chunks to unbond with the pool account,
		/// [`Call::pool_withdraw_unbonded`] can be called to try and minimize unlocking chunks.
		/// The [`StakingInterface::unbond`] will implicitly call [`Call::pool_withdraw_unbonded`]
		/// to try to free chunks if necessary (ie. if unbound was called and no unlocking chunks
		/// are available). However, it may not be possible to release the current unlocking chunks,
		/// in which case, the result of this call will likely be the `NoMoreChunks` error from the
		/// staking system.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2257-2296)
```rust
		pub fn unbond(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			#[pallet::compact] unbonding_points: BalanceOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			// ensure member is not in an un-migrated state.
			ensure!(
				!Self::api_member_needs_delegate_migration(member_account.clone()),
				Error::<T>::NotMigrated
			);

			let (mut member, mut bonded_pool, mut reward_pool) =
				Self::get_member_with_pools(&member_account)?;

			bonded_pool.ok_to_unbond_with(&who, &member_account, &member, unbonding_points)?;

			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L172-175)
```rust
	/// See [`StakingInterface::unbond`].
	fn unbond(pool_account: Pool<Self::AccountId>, amount: Self::Balance) -> DispatchResult {
		Self::CoreStaking::unbond(&pool_account.0, amount)
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L1385-1413)
```rust
	/// Unbonds a controller.
	pub(crate) fn do_unbond(
		controller: T::AccountId,
		value: BalanceOf<T>,
	) -> Result<Option<Weight>, DispatchError> {
		let unlocking = Self::ledger(Controller(controller.clone())).map(|l| l.unlocking.len())?;

		// if there are no unlocking chunks available, try to withdraw chunks older than
		// `BondingDuration` to proceed with the unbonding.
		let maybe_withdraw_weight = {
			if unlocking == T::MaxUnlockingChunks::get() as usize {
				let real_num_slashing_spans =
					SlashingSpans::<T>::get(&controller).map_or(0, |s| s.iter().count());
				Some(Self::do_withdraw_unbonded(&controller, real_num_slashing_spans as u32)?)
			} else {
				None
			}
		};

		// we need to fetch the ledger again because it may have been mutated in the call
		// to `Self::do_withdraw_unbonded` above.
		let mut ledger = Self::ledger(Controller(controller))?;
		let mut value = value.min(ledger.active);
		let stash = ledger.stash.clone();

		ensure!(
			ledger.unlocking.len() < T::MaxUnlockingChunks::get() as usize,
			Error::<T>::NoMoreChunks,
		);
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2011-2023)
```rust
				let era =
					session_rotation::Rotator::<T>::active_era().saturating_add(unbond_duration);
				if let Some(chunk) = ledger.unlocking.last_mut().filter(|chunk| chunk.era == era) {
					// To keep the chunk count down, we only keep one chunk per era. Since
					// `unlocking` is a FiFo queue, if a chunk exists for `era` we know that it will
					// be the last one.
					chunk.value = chunk.value.defensive_saturating_add(value)
				} else {
					ledger
						.unlocking
						.try_push(UnlockChunk { value, era })
						.map_err(|_| Error::<T>::NoMoreChunks)?;
				};
```

**File:** substrate/frame/election-provider-multi-phase/test-staking-e2e/src/lib.rs (L344-362)
```rust
		// currently unlocking 0 chunks in the bonded pools ledger.
		assert_eq!(unlocking_chunks_of(pool_bonded_account), 0);

		// unbond 2 from pool.
		assert_ok!(Pools::unbond(RuntimeOrigin::signed(2), 2, 10));

		// amount is still locked in the pool, needs to wait for unbonding period.
		assert_eq!(staked_amount_for(pool_bonded_account), 25);

		// max chunks in the ledger are now filled up (`MaxUnlockingChunks == 1`).
		assert_eq!(unlocking_chunks_of(pool_bonded_account), 1);

		// tries to unbond 3 from pool. it will fail since there are no unlocking chunks left
		// available and the current in the queue haven't been there for more than bonding
		// duration.
		assert_err!(
			Pools::unbond(RuntimeOrigin::signed(3), 3, 10),
			pallet_staking::Error::<Runtime>::NoMoreChunks
		);
```

**File:** substrate/frame/staking-async/src/tests/bonding.rs (L685-727)
```rust
#[test]
fn unbonding_rejects_if_max_chunks() {
	ExtBuilder::default()
		.max_unlock_chunks(3)
		.bonding_duration(7)
		.build_and_execute(|| {
			// given
			assert_eq!(
				Staking::ledger(11.into()).unwrap(),
				StakingLedgerInspect {
					stash: 11,
					total: 1000,
					active: 1000,
					unlocking: Default::default(),
				}
			);

			// when
			Staking::unbond(RuntimeOrigin::signed(11), 250).unwrap();
			Session::roll_until_active_era(2);
			Staking::unbond(RuntimeOrigin::signed(11), 250).unwrap();
			Session::roll_until_active_era(3);
			Staking::unbond(RuntimeOrigin::signed(11), 250).unwrap();

			// then
			assert_eq!(
				Staking::ledger(11.into()).unwrap(),
				StakingLedgerInspect {
					stash: 11,
					total: 1000,
					active: 250,
					unlocking: bounded_vec![
						UnlockChunk { value: 250, era: 1 + 7 },
						UnlockChunk { value: 250, era: 2 + 7 },
						UnlockChunk { value: 250, era: 3 + 7 },
					],
				},
			);

			// when
			Session::roll_until_active_era(4);
			assert_noop!(Staking::unbond(RuntimeOrigin::signed(11), 100), Error::<T>::NoMoreChunks,);
		});
```
