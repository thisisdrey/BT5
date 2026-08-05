## Title
`pallet-fast-unstake` permanently halts all queued unstake processing when a single stash's reserved deposit cannot be fully unreserved - ([File: substrate/frame/fast-unstake/src/lib.rs])

### Summary
The external report describes a Vault design flaw where a check performed at *request time* (user has sufficient LP balance) is not re-validated atomically at *settlement time*, so a single malicious request can make settlement revert and freeze the entire pending queue for all other users. `pallet-fast-unstake` has an analogous "request now, settle later" pattern: `register_fast_unstake` reserves a fixed `Deposit` on a stash at registration time and places the stash into a shared `Queue`/`Head`. When the pallet later tries to release that deposit — either via the user-facing `deregister` extrinsic or during automated `on_idle` batch processing (`unstake_stash`) — it calls `T::Currency::unreserve`. If the returned "remaining" (unfulfilled) amount is non-zero, the pallet calls `Self::halt`, which sets `ErasToCheckPerBlock` to `0`. This permanently stops **all** further registration, deregistration, and `on_idle` processing for **every** stash in the queue/head until a privileged `ControlOrigin` manually re-enables it via `control`.

### Finding Description
`register_fast_unstake` reserves the deposit and enqueues the stash: [1](#0-0) 

The reservation amount tracked by `Currency::reserve`/`unreserve` in the legacy `Currency` trait is not earmarked per-purpose; it is a single aggregate `reserved` balance field on the account. Anything that reduces that aggregate reserved balance below the amount fast-unstake believes it holds (e.g. the account's balance being reaped/dusted when free balance falls below the existential deposit, other reserves on the same account being slashed, or any other legitimate/adversarial reduction of the account's total reserved funds after registration but before processing) will cause a subsequent `unreserve(deposit)` call to return a non-zero remainder.

Both code paths that release the deposit treat a non-zero remainder as a fatal, pallet-wide condition rather than an isolated per-user failure:

`deregister` (public, signed, permissionless for the registrant): [2](#0-1) 

`on_idle` batch unstaking (`unstake_stash` closure, runs automatically every block once a batch is checked): [3](#0-2) 

`halt` disables the entire pallet, not just the offending stash: [4](#0-3) 

Once `ErasToCheckPerBlock` is `0`, both public entrypoints are gated shut and `on_idle` processing exits immediately: [5](#0-4) [6](#0-5) [7](#0-6) 

This is the same broken invariant as the Vault bug: a balance-sufficiency assumption checked once at request time (reserve succeeds) is not guaranteed to hold at settlement time (unreserve fully succeeds), and instead of gracefully handling the shortfall for that one stash, the pallet takes down processing for the whole shared queue — with no automatic recovery path (only a privileged `ControlOrigin` call to `control` can restore `ErasToCheckPerBlock`).

### Impact Explanation
This falls squarely within "permanent user-fund or bridge-state lock" and "public underpriced work that … stalls … processing": every other stash already sitting in `Queue` or `Head` (having already been fully unbonded and chilled by `register_fast_unstake`, i.e. removed from active staking) becomes stuck — their deposits remain reserved and their unbonded funds cannot progress through fast-unstake — until governance/`ControlOrigin` intervenes. This is a denial-of-service against the entire fast-unstake subsystem triggered by conditions affecting a single account, with no attacker needing to be a validator, collator, relayer, or privileged actor.

### Likelihood Explanation
The trigger condition (`T::Currency::unreserve` returning a non-zero remainder) requires the affected account's aggregate reserved balance to fall below the tracked fast-unstake deposit between registration and processing. Because `Currency::reserve`/`unreserve` on an account is a single unified counter shared across all pallets using it (not earmarked per purpose), this can occur through ordinary interactions with other reserve-consuming pallets on the same account, or through balance dusting/reaping if free balance drops below the existential deposit. I could not fully verify, within the available indexed code, the exact current dusting semantics for `pallet-balances` regarding whether reserved balance survives an account being reaped (this depends on the specific `Currency`/`fungible` trait wiring configured in the runtime that uses `pallet-fast-unstake`), so the precise trigger mechanics are runtime/configuration dependent. This uncertainty affects confidence in *how easily* an attacker can force the underflow, but the destructive pallet-wide consequence once triggered is unambiguous and directly present in the code shown above.

### Recommendation
- Do not use a global `halt()` in response to a per-stash unreserve shortfall. Instead, isolate the failure: drop/skip only the affected stash (emit an error event, forfeit or best-effort partially unreserve, and continue processing the rest of the batch/queue), mirroring the Vault fix pattern of custody-then-partial-refund rather than reverting/halting the shared settlement path.
- Consider using `Currency::hold`/`Fungible::hold` with a dedicated `HoldReason` for the fast-unstake deposit instead of the legacy unearmarked `reserve`, so that other pallets/operations on the same account cannot deplete the amount fast-unstake believes is dedicated to it.
- If a defensive halt is still desired for truly unexpected states, scope it to removing only the malformed entry from `Queue`/`Head` rather than zeroing `ErasToCheckPerBlock` for the whole pallet, and/or add an automatic/permissionless recovery path instead of requiring `ControlOrigin`.

### Proof of Concept
1. Attacker's controller account calls `register_fast_unstake`, which reserves `T::Deposit` on the stash and inserts it into `Queue`.
2. Before fast-unstake processing reaches this stash, the attacker arranges for the stash account's aggregate reserved balance to be reduced below `T::Deposit` (e.g., by causing the account to be dusted/reaped via depleting its free balance below the existential deposit, or via interaction with another pallet that shares the same reserved-balance counter on that account).
3. Either:
   - a) the attacker calls `deregister` themselves, hitting `substrate/frame/fast-unstake/src/lib.rs:382-387`, where `T::Currency::unreserve` returns a non-zero `remaining`, invoking `Self::halt("not enough balance to unreserve")`; or
   - b) they simply wait for `on_idle` to process their batch, hitting the identical check in `unstake_stash` at `substrate/frame/fast-unstake/src/lib.rs:555-564`.
4. `halt` sets `ErasToCheckPerBlock::<T>::put(0)` (`substrate/frame/fast-unstake/src/lib.rs:426`).
5. From this point on, `register_fast_unstake` and `deregister` both revert with `Error::<T>::CallNotAllowed` (lines 333, 374), and `do_on_idle` exits immediately without processing anything (lines 447-450) — freezing every other stash already queued or in `Head`, exactly analogous to the Vault settlement freeze, until a `ControlOrigin` account manually calls `control` to restore a non-zero `ErasToCheckPerBlock`.

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

**File:** substrate/frame/fast-unstake/src/lib.rs (L376-390)
```rust
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
