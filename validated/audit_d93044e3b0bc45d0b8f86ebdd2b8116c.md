Based on my research, I found a strong, provable local analog to the "processEpoch() needs to be called regularly" bug class in `pallet-fast-unstake`. The core broken invariant in the Solidity report is: *a critical state-advancing operation is designed to require regular external triggering, but nothing in the protocol guarantees that triggering happens, and if it's skipped, user state desynchronizes and funds become stuck.* The same pattern exists in `pallet-fast-unstake`, where the queue-processing engine is entirely `on_idle`-gated (best-effort, unguaranteed leftover block weight) rather than deterministically serviced, while the corresponding user-facing dispatchable has already made an irreversible, fund-affecting state change (chill + full unbond + reserve) before that processing occurs.

### Title
Fast-unstake requests are irreversibly unbonded/chilled and reserve-locked but depend entirely on unguaranteed `on_idle` leftover weight to ever be processed - (File: `substrate/frame/fast-unstake/src/lib.rs`)

### Summary
`pallet_fast_unstake::register_fast_unstake` immediately chills the staker, fully unbonds their stash, and reserves a deposit — all before any correctness check runs. The actual "processing" (checking exposure across eras and finally unstaking) is executed exclusively from `Hooks::on_idle`, which by design only fires with whatever weight happens to remain unused after normal block execution. The pallet's own documentation admits this "provides no guarantee about when it will succeed, if at all." This mirrors the reported class of bug: a state machine that must be advanced regularly by some outside trigger, but for which no protocol-level guarantee of that trigger exists, and whose desynchronization directly harms users holding funds in the pending state.

### Finding Description
`register_fast_unstake` performs the fund-affecting actions synchronously and unconditionally on call: [1](#0-0) 

This chills the account, fully unbonds the stash (losing eligibility for staking rewards from that point on), and reserves `T::Deposit::get()` — before any eligibility check for fast-unstake has actually run. The eligibility check and eventual release of funds only happen in `do_on_idle`, invoked exclusively from the `on_idle` hook: [2](#0-1) 

`do_on_idle` bails out immediately if `remaining_weight` (i.e., whatever weight is left over after all other extrinsics/hooks in the block) is insufficient, and there is no other public dispatchable, scheduler alarm, or `on_initialize` guarantee that forces this queue forward: [3](#0-2) [4](#0-3) 

The pallet's own module documentation explicitly acknowledges the exact invariant break described in the external report — that progress is not guaranteed and is entirely dependent on external/incidental conditions (leftover weight): [5](#0-4) 

Once a request has been picked up from `Queue` into `Head` (i.e., a batch has begun being checked), the user can no longer self-service or cancel: [6](#0-5) 

If block space is consistently saturated — e.g., an unprivileged party submits/produces enough extrinsics each block to leave `remaining_weight` below `T::DbWeight::get().reads(2)` — `on_idle` never executes any fast-unstake work at all. Every account that has called `register_fast_unstake` is left indefinitely: fully unbonded (no active stake, earning zero rewards) and with its deposit permanently reserved, since deregistration is also gated by `ErasToCheckPerBlock::<T>::get() != 0` and is unavailable once the request reaches `Head`. This is functionally identical to the `PublicVault` bug: an update mechanism ("processEpoch"/"do_on_idle") that must run "regularly" for the system's accounting to stay correct, but which has no enforced cadence, while dependent user funds are already committed based on the assumption that it will run.

### Impact Explanation
Because `register_fast_unstake` performs irreversible unbonding/chilling *before* the processing guarantee exists, any sustained inability to obtain `on_idle` leftover weight (organic congestion, or a cheap griefing strategy of consistently filling blocks to near-capacity) causes every queued/head account's funds to sit unbonded and reserve-locked indefinitely, with no forced-progress path and no self-service exit once in `Head`. This matches the "permanent user-fund or bridge-state lock" and "public underpriced work that degrades block production or stalls ... processing" categories in the impact gate — the queue is public work that has no deterministic weight allocation, exactly the failure mode the report is warning about for `processEpoch`.

### Likelihood Explanation
No malicious validator, collator, relayer, or admin is required — an ordinary signed account calling `register_fast_unstake` is enough to enter the exposed state, and the stalling condition (near-full blocks) is a naturally occurring or cheaply inducible network condition rather than a privileged action. The pallet authors themselves flag the non-guarantee in the doc comments, indicating this is a known architectural property rather than a hardened, bounded-time mechanism.

### Recommendation
Do not perform the irreversible chill/unbond/reserve step in `register_fast_unstake` before the processing guarantee is established, or alternatively provide a deterministic per-block minimum weight allocation (similar to `T::ServiceWeight` in `pallet-message-queue`'s `on_initialize`, rather than relying purely on `IdleMaxServiceWeight`/`on_idle`) so that fast-unstake queue processing advances by a bounded, non-zero amount every N blocks regardless of block congestion. Additionally, allow deregistration/recovery for `Head` entries stuck beyond some maximum wait time, so users are not permanently reliant on incidental leftover block weight to recover their funds.

### Proof of Concept
1. Account `A` calls `register_fast_unstake`: stash is chilled, fully unbonded, and `Deposit` reserved — see [1](#0-0) .
2. From this point on, `A`'s recovery/finalization depends solely on `Pallet::on_idle` being invoked with `remaining_weight` that clears the early-exit threshold in [2](#0-1)  and the weight-vs-work check in [4](#0-3) .
3. If blocks are consistently filled close to `max_block` (by any combination of ordinary transaction traffic), `remaining_weight` passed to `on_idle` stays below the threshold every block, so `do_on_idle` never advances `A`'s request.
4. `A` cannot recover: deregistration is unavailable once picked into `Head` per [6](#0-5) , and even in `Queue` it requires `ErasToCheckPerBlock::<T>::get() != 0`, giving no forced-progress or timeout-based exit. `A`'s stash remains unbonded (earning nothing) and the deposit remains reserved indefinitely, exactly analogous to the `PublicVault.processEpoch()` lag scenario in the external report.

### Citations

**File:** substrate/frame/fast-unstake/src/lib.rs (L80-86)
```rust
//! ## Low Level / Implementation Details
//!
//! This pallet works off the basis of `on_idle`, meaning that it provides no guarantee about when
//! it will succeed, if at all. Moreover, the queue implementation is unordered. In case of
//! congestion, no FIFO ordering is provided.
//!
//! A few important considerations can be concluded based on the `on_idle`-based implementation:
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L268-276)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		fn on_idle(_: BlockNumberFor<T>, remaining_weight: Weight) -> Weight {
			if remaining_weight.any_lt(T::DbWeight::get().reads(2)) {
				return Weight::from_parts(0, 0);
			}

			Self::do_on_idle(remaining_weight)
		}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L330-349)
```rust
		pub fn register_fast_unstake(origin: OriginFor<T>) -> DispatchResult {
			let ctrl = ensure_signed(origin)?;

			ensure!(ErasToCheckPerBlock::<T>::get() != 0, Error::<T>::CallNotAllowed);
			let stash_account =
				T::Staking::stash_by_ctrl(&ctrl).map_err(|_| Error::<T>::NotController)?;
			ensure!(!Queue::<T>::contains_key(&stash_account), Error::<T>::AlreadyQueued);
			ensure!(!Self::is_head(&stash_account), Error::<T>::AlreadyHead);
			ensure!(!T::Staking::is_unbonding(&stash_account)?, Error::<T>::NotFullyBonded);

			// chill and fully unstake.
			T::Staking::chill(&stash_account)?;
			T::Staking::fully_unbond(&stash_account)?;

			T::Currency::reserve(&stash_account, T::Deposit::get())?;

			// enqueue them.
			Queue::<T>::insert(stash_account, T::Deposit::get());
			Ok(())
		}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L369-390)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(<T as Config>::WeightInfo::deregister())]
		pub fn deregister(origin: OriginFor<T>) -> DispatchResult {
			let ctrl = ensure_signed(origin)?;

			ensure!(ErasToCheckPerBlock::<T>::get() != 0, Error::<T>::CallNotAllowed);

			let stash_account =
				T::Staking::stash_by_ctrl(&ctrl).map_err(|_| Error::<T>::NotController)?;
			ensure!(Queue::<T>::contains_key(&stash_account), Error::<T>::NotQueued);
			ensure!(!Self::is_head(&stash_account), Error::<T>::AlreadyHead);
			let deposit = Queue::<T>::take(stash_account.clone());

			if let Some(deposit) = deposit.defensive() {
				let remaining = T::Currency::unreserve(&stash_account, deposit);
				if !remaining.is_zero() {
					Self::halt("not enough balance to unreserve");
				}
			}

			Ok(())
		}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L443-450)
```rust
		pub(crate) fn do_on_idle(remaining_weight: Weight) -> Weight {
			// any weight that is unaccounted for
			let mut unaccounted_weight = Weight::from_parts(0, 0);

			let eras_to_check_per_block = ErasToCheckPerBlock::<T>::get();
			if eras_to_check_per_block.is_zero() {
				return T::DbWeight::get().reads(1).saturating_add(unaccounted_weight);
			}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L473-476)
```rust
			if max_weight(validator_count, next_batch_size).any_gt(remaining_weight) {
				log!(debug, "early exit because eras_to_check_per_block is zero");
				return T::DbWeight::get().reads(3).saturating_add(unaccounted_weight);
			}
```
