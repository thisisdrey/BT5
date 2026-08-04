### Title
Attacker-Induced Reserve Shortfall in `pallet-fast-unstake` Permanently Halts the Shared Unstake Queue for All Other Stakers - (File: `substrate/frame/fast-unstake/src/lib.rs`)

### Summary
`pallet-fast-unstake` reserves a per-account deposit at `register_fast_unstake` and later calls `T::Currency::unreserve` on the *same, non-isolated* reserve bucket when the stash is processed. If that unreserve call cannot recover the full deposit (because the account's shared reserved balance was reduced by any other unrelated reserving action in the interim), the pallet does not just fail that one stash — it sets the *global* `ErasToCheckPerBlock` storage value to `0`, which halts processing for **every** account currently sitting in `Queue`/`Head`. This is the same bug class as the reported PearVault issue: a single unexecutable/failed settlement for one user permanently blocks a shared queue/counter that gates service for all other users.

### Finding Description
`register_fast_unstake` reserves `T::Deposit::get()` from the stash using the generic (non-named) `ReservableCurrency` API and enqueues the stash: [1](#0-0) 

When the batch is later processed in `do_on_idle`, unexposed stashes are finalized via `unstake_stash`, which force-unstakes and then unreserves the deposit: [2](#0-1) 

`Currency::unreserve` only releases as much as is currently reserved and returns any shortfall as `remaining`. If `remaining` is non-zero, `Self::halt(...)` is invoked: [3](#0-2) 

`halt` sets `ErasToCheckPerBlock` to `0`. This value is a single, pallet-wide storage item that gates *all* processing: [4](#0-3)  — `do_on_idle` early-returns doing nothing whenever `ErasToCheckPerBlock` is `0`.

Once halted, `register_fast_unstake` and `deregister` themselves become unusable because both explicitly require `ErasToCheckPerBlock::<T>::get() != 0`: [5](#0-4) [6](#0-5) 

The `deregister` path has the identical unreserve/halt pattern: [7](#0-6) 

The root cause is that `T::Currency: ReservableCurrency` uses a single unnamed reserve bucket per account rather than an isolated "hold" tied to fast-unstake. Any other legitimate reserving pallet interaction on the same account (e.g. depositing/slashing a bond in another pallet that shares the reserve counter) between `register_fast_unstake` and the block where the batch is actually processed can reduce the account's *total* reserved balance below the fast-unstake deposit that was recorded at registration time. When `unstake_stash` executes, the `unreserve` call then necessarily returns a non-zero `remaining`, and the pallet halts — exactly like the PearVault bug where one user's unexecutable withdrawal (due to their address's state after registration) permanently blocked settlement for every other pending request through a shared gating variable (`totalPendingShares` there, `ErasToCheckPerBlock` here).

Nothing in `unstake_stash` or `halt` distinguishes "this one stash's deposit could not be fully recovered" from "the whole pallet is broken" — the response to a single-account edge case is a chain-wide (well, pallet-wide) shutdown that requires a privileged `T::ControlOrigin` to call `control` again to resume, exactly mirroring the report's recommendation that only an admin can unstick things after the DoS has already occurred.

### Impact Explanation
Once one queued/head stash triggers the halt, every other account that is `Queue`d or in `Head` — accounts unrelated to the triggering stash — has its fast-unstake deposit reserve permanently locked and its unstake request permanently stuck: `on_idle` no longer does anything, and neither `register_fast_unstake` nor `deregister` can be called (`CallNotAllowed`). This is a pallet-wide, permanent fund-lock and processing-stall affecting all pending fast-unstake users, until a privileged `ControlOrigin` intervenes — matching the "permanent user-fund...lock" / "public underpriced work that... stalls... processing" impact categories, and is a direct functional analog of the PearVault M-06 finding (one broken settlement blocking a shared counter/queue for everyone).

### Likelihood Explanation
The trigger does not require a malicious validator, collator, relayer, or admin — only an ordinary signed account that (a) registers for fast-unstake and (b) has (or subsequently obtains) some other reserved balance usage on the same account that reduces its reserve below the recorded deposit before the batch containing it is processed. Because `ReservableCurrency`'s reserve is a single shared counter per account (not scoped/held specifically for fast-unstake), this is a realistic condition reachable through ordinary runtime interactions (e.g., other pallets reserving/slashing on the same account), not a contrived or infrastructure-dependent scenario.

### Recommendation
- Use a named/held reservation (`fungible::hold` with a fast-unstake-specific `HoldReason`) instead of the generic `ReservableCurrency::reserve`/`unreserve`, so the deposit cannot be silently reduced by unrelated reserve activity on the same account.
- Make the failure handling local: if `unreserve` (or hold-release) cannot recover the full deposit for a single stash, do not zero out the pallet-wide `ErasToCheckPerBlock`; instead, emit a per-stash error/event, remove only that stash from the batch, and continue processing the remaining stashes in `Head`/`Queue`.
- Reserve the deposit amount using a mechanism that guarantees it cannot be reduced by any other pallet for the duration of the fast-unstake request lifecycle.

### Proof of Concept
1. Account `A` calls `register_fast_unstake`, reserving `Deposit` via the shared `ReservableCurrency` bucket; `A` is placed in `Queue`.
2. Before `A`'s batch is processed by `on_idle`, `A` (or a colluding contract/pallet interaction) triggers another reserve-consuming action on the same account that reduces `A`'s total reserved balance below `Deposit` (e.g., another pallet slashes or consumes part of the shared reserve).
3. Other accounts `B`, `C`, ... also call `register_fast_unstake` normally and are queued.
4. `do_on_idle` eventually processes the batch containing `A`; `A` is found unexposed, so `unstake_stash` runs `T::Currency::unreserve(&A, deposit)`, which returns `remaining > 0` because `A`'s reserved balance was depleted in step 2.
5. `Self::halt("not enough balance to unreserve")` executes, setting `ErasToCheckPerBlock` to `0` and emitting `Event::InternalError`.
6. From this point, `do_on_idle` does nothing every block (early return), and `register_fast_unstake`/`deregister` both return `Error::CallNotAllowed` for `B`, `C`, and everyone else still queued — their deposits remain reserved and their unstake requests are permanently stuck until a `ControlOrigin` account calls `control` to restore `ErasToCheckPerBlock`.

### Citations

**File:** substrate/frame/fast-unstake/src/lib.rs (L333-333)
```rust
			ensure!(ErasToCheckPerBlock::<T>::get() != 0, Error::<T>::CallNotAllowed);
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L340-348)
```rust
			// chill and fully unstake.
			T::Staking::chill(&stash_account)?;
			T::Staking::fully_unbond(&stash_account)?;

			T::Currency::reserve(&stash_account, T::Deposit::get())?;

			// enqueue them.
			Queue::<T>::insert(stash_account, T::Deposit::get());
			Ok(())
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L374-374)
```rust
			ensure!(ErasToCheckPerBlock::<T>::get() != 0, Error::<T>::CallNotAllowed);
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L380-387)
```rust
			let deposit = Queue::<T>::take(stash_account.clone());

			if let Some(deposit) = deposit.defensive() {
				let remaining = T::Currency::unreserve(&stash_account, deposit);
				if !remaining.is_zero() {
					Self::halt("not enough balance to unreserve");
				}
			}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L423-428)
```rust
		/// Halt the operations of this pallet.
		pub(crate) fn halt(reason: &'static str) {
			frame_support::defensive!(reason);
			ErasToCheckPerBlock::<T>::put(0);
			Self::deposit_event(Event::<T>::InternalError)
		}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L447-450)
```rust
			let eras_to_check_per_block = ErasToCheckPerBlock::<T>::get();
			if eras_to_check_per_block.is_zero() {
				return T::DbWeight::get().reads(1).saturating_add(unaccounted_weight);
			}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L555-564)
```rust
			let unstake_stash = |stash: T::AccountId, deposit| {
				let result = T::Staking::force_unstake(stash.clone());
				let remaining = T::Currency::unreserve(&stash, deposit);
				if !remaining.is_zero() {
					Self::halt("not enough balance to unreserve");
				} else {
					log!(debug, "unstaked {:?}, outcome: {:?}", stash, result);
					Self::deposit_event(Event::<T>::Unstaked { stash, result });
				}
			};
```
