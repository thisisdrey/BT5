## Analysis

The report's core broken invariant: an unprivileged actor can repeatedly perform cheap round-trip actions (deposit → immediate redeem) that consume a **shared, bounded liquidity buffer**, causing legitimate users' redemption/unbond calls to revert with no fair ordering guarantee.

The closest verifiable local analog is in `pallet-nomination-pools`: every member's `unbond` call operates against the **pool's single shared bonded (stash) account**, whose `StakingLedger.unlocking` vector is bounded by `T::MaxUnlockingChunks` in `pallet-staking`. This bound is a shared resource across *all* members of the pool, not a per-member limit.

### Title
Shared pool bonded-account unlocking-chunk buffer can be permanently exhausted by a single member, permanently locking all other members' withdrawals - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pools::unbond` forwards every member's unbonding request to the pool's single underlying staking ledger via `T::StakeAdapter::unbond` [1](#0-0) . That ledger's `unlocking` chunk list is capped by `T::MaxUnlockingChunks`, a chain-wide constant, and is **shared by every member of the pool** because they all unbond through the same pool bonded account. This is the exact "buffer" analog of the LST adapter's redemption buffer in the external report: a bounded, shared resource that legitimate users compete for, with no fairness/queueing guarantee, and requests that don't fit are simply reverted (`NoMoreChunks`) rather than queued.

### Finding Description
`pallet_staking`'s `do_unbond` enforces the chunk cap on the ledger belonging to the controller/stash being unbonded: [2](#0-1) 

For a nomination pool, the "stash" being unbonded is always the pool's bonded account, shared by all members — the nomination-pools pallet itself documents this exact hazard: [3](#0-2) 

Because chunks are deduplicated per-era (`ledger.unlocking.last_mut().filter(|chunk| chunk.era == era)`), an attacker only needs to submit **one minimal `unbond` call per era** to guarantee that a fresh chunk occupies a slot in that era, keeping the shared `unlocking` list permanently at `MaxUnlockingChunks`. Any other pool member attempting to `unbond` in an era where the attacker has already claimed the day's chunk slot (or where no chunk has yet expired past `BondingDuration`) receives `Error::NoMoreChunks`, exactly mirroring the external report's "unsuccessful withdrawal requests are just reverted, forcing users to re-join the competition."

The pallet's own auto-mitigation (`pool_withdraw_unbonded`, callable by anyone) only clears chunks whose era is already past `BondingDuration`; it cannot prevent the attacker from immediately re-occupying the freed slot in the same era with another dust-sized `unbond`, since `pallet-staking`'s `unbond` auto-invokes withdrawal-and-retry only in the entrant's own transaction, not preemptively for the whole ledger: [4](#0-3) 

### Impact Explanation
This satisfies the "permanent user-fund or bridge-state lock" impact category: an unprivileged member of a nomination pool can indefinitely prevent every other member of that pool from ever unbonding/withdrawing their stake, by paying only the dust cost of a minimal `unbond` transaction once per era. Funds remain bonded and inaccessible to legitimate members for as long as the attacker sustains the pattern, which is cheap relative to the value locked in a pool.

### Likelihood Explanation
The attack requires no privileged role, validator/collator status, governance action, or malicious infrastructure — only being a member of the target pool (trivially achievable via `Pools::join`) and submitting one low-value `unbond` transaction per era. `MaxUnlockingChunks` is a fixed, well-known runtime constant (commonly small, e.g. 32), making the number of eras needed to first saturate the buffer modest and the recurring cost of maintaining saturation negligible (one small transaction per era).

### Recommendation
Do not treat the pool bonded account's `MaxUnlockingChunks` slots as a first-come-first-served shared resource with only a revert-and-retry fallback. Introduce per-pool internal accounting/queueing of member unbond requests independent of the underlying staking ledger's chunk cap (e.g., batch/merge member unbonds destined for the same era before submitting to `pallet-staking`, or maintain an internal request queue that drains proportionally to freed chunk capacity) so that no single member can monopolize the shared chunk slots indefinitely.

### Proof of Concept
1. Attacker joins pool `P` with a minimal bond.
2. Every era `e`, attacker calls `Pools::unbond(attacker, attacker, 1)` before other members' transactions land, creating/renewing a chunk for era `e + BondingDuration` in the pool's shared `StakingLedger.unlocking` [5](#0-4) .
3. Once `unlocking.len() == MaxUnlockingChunks` is sustained across eras (attacker always refills the slot vacated by auto-withdraw before/at the same time other members transact), any other member `M` calling `Pools::unbond(M, M, amount)` hits `ensure!(ledger.unlocking.len() < T::MaxUnlockingChunks::get() as usize, Error::<T>::NoMoreChunks)` and reverts [6](#0-5) .
4. Member `M`'s funds remain bonded in the pool indefinitely, unable to be unbonded, as long as attacker repeats step 2.

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2295)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2339-2367)
```rust
		/// Call `withdraw_unbonded` for the pools account. This call can be made by any account.
		///
		/// This is useful if there are too many unlocking chunks to call `unbond`, and some
		/// can be cleared by withdrawing. In the case there are too many unlocking chunks, the user
		/// would probably see an error like `NoMoreChunks` emitted from the staking system when
		/// they attempt to unbond.
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::pool_withdraw_unbonded(*num_slashing_spans))]
		pub fn pool_withdraw_unbonded(
			origin: OriginFor<T>,
			pool_id: PoolId,
			num_slashing_spans: u32,
		) -> DispatchResult {
			ensure_signed(origin)?;
			// ensure pool is not in an un-migrated state.
			ensure!(!Self::api_pool_needs_delegate_migration(pool_id), Error::<T>::NotMigrated);

			let pool = BondedPool::<T>::get(pool_id).ok_or(Error::<T>::PoolNotFound)?;

			// For now we only allow a pool to withdraw unbonded if its not destroying. If the pool
			// is destroying then `withdraw_unbonded` can be used.
			ensure!(pool.state != PoolState::Destroying, Error::<T>::NotDestroying);
			T::StakeAdapter::withdraw_unbonded(
				Pool::from(pool.bonded_account()),
				num_slashing_spans,
			)?;

			Ok(())
		}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L1390-1413)
```rust
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

**File:** substrate/frame/staking/src/pallet/impls.rs (L1436-1450)
```rust
			// Note: in case there is no current era it is fine to bond one era more.
			let era = CurrentEra::<T>::get()
				.unwrap_or(0)
				.defensive_saturating_add(T::BondingDuration::get());
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
