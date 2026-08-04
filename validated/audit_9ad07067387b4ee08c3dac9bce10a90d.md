### Title
`debug_assert!` on external `Registrar::make_parachain`/`make_parathread` calls can panic the mandatory `on_initialize` hook, halting block production - ([File: polkadot/runtime/common/src/slots/mod.rs])

### Summary
The Slots pallet's `manage_lease_period_start`, called unconditionally from the pallet's `on_initialize` hook at the start of every new lease period, invokes `T::Registrar::make_parachain` / `T::Registrar::make_parathread` and only checks the result with `debug_assert!(res.is_ok())` instead of handling the `Err` case gracefully. This mirrors the `_settleAuction()` bug: an unchecked, revert-prone external call embedded in an unconditional state-transition path can turn a single "unhappy path" outcome into a much larger denial-of-service — here, a halted chain rather than a stuck auction house.

### Finding Description
`manage_lease_period_start` in `polkadot/runtime/common/src/slots/mod.rs` runs every lease-period boundary as part of `Hooks::on_initialize`: [1](#0-0) 

Inside it, for paras entering/leaving parachain status it calls the `Registrar` trait's `make_parachain`/`make_parathread`, asserting success only via `debug_assert!`: [2](#0-1) 

`make_parachain`/`make_parathread` are not infallible — the concrete `paras_registrar` implementation explicitly returns `Error::<T>::NotParathread` / `Error::<T>::NotParachain` (via `CannotUpgrade`/`CannotDowngrade`) whenever the para's current on-chain lifecycle state (`paras::Pallet::lifecycle`) does not match the expected precondition at call time: [3](#0-2) 

The lifecycle of a para (`Parathread` vs `Parachain`) is not exclusively controlled by the Slots pallet. It can be mutated independently through other public dispatchables/pallets that share the same `Registrar` — e.g. `paras_registrar::swap`, `deregister`, or the `assigned_slots` pallet's own `Registrar::make_parachain`/`make_parathread` calls, all of which operate on the very same para lifecycle state: [4](#0-3) 

If, between the block where Slots decided a para should transition and the block where `manage_lease_period_start` actually executes, an unrelated signed extrinsic (swap, deregister, or an assigned-slot life-cycle change) has already flipped that para's lifecycle in a way Slots did not anticipate, the `Registrar::make_parachain`/`make_parathread` call inside `manage_lease_period_start` returns `Err`. In any binary built with `debug-assertions = true` (as used for testnets, try-runtime, and several CI/staging configurations), this `Err` trips the `debug_assert!`, panicking inside `on_initialize` — a hook that runs unconditionally and outside of the recoverable-dispatch machinery. A panic there aborts block execution for that block, effectively bricking further block production, exactly analogous to how an unchecked `safeTransferFrom` revert bricked the entire auction house rather than just the failing operation.

### Impact Explanation
This is high impact because the corrupted precondition is asserted with `debug_assert!` rather than handled via a `Result`/graceful branch (contrast this with `manage_auction_end`, which correctly `match`es every `LeaseError` variant and never panics). A panic inside a mandatory, non-dispatchable `on_initialize` hook can stall or crash block production network-wide on any node/runtime built with debug assertions enabled, which is a stronger "chain-bricking" analog to the original NFT-auction DoS than a per-extrinsic revert.

### Likelihood Explanation
Likelihood depends on runtime lifecycle races between Slots-driven transitions and other unprivileged/permissioned dispatchables (`swap`, `deregister`, assigned-slots operations) touching the same para ID within the same lease-period window, and on whether the deployed binary has `debug-assertions` enabled. The search for this repository's build profile did not turn up an explicit `debug-assertions = true` in the root `Cargo.toml` release profile, so likelihood on a genuine production mainnet build is uncertain/likely lower there; but it remains a real, exploitable defect on any debug-assertions-enabled deployment (common for testnets and CI), and the underlying logic bug (unchecked external call outcome on a shared mutable precondition) is a real code smell independent of build profile.

### Recommendation
Replace `debug_assert!(res.is_ok())` with explicit error handling that never panics: skip/log the failed transition, emit an event, and let governance intervene (mirroring the pattern already used in `manage_auction_end`/`Auctions::on_initialize`), or make the desired state idempotent/self-healing before applying it, instead of relying on an assertion that both target lifecycle states will always align.

### Proof of Concept
1. Attacker (or any unprivileged/permissioned agent) triggers a Slots-tracked para to be scheduled for a lifecycle change at the upcoming lease-period boundary (e.g., ends up in `parachains` list for `manage_lease_period_start`'s "incoming" set).
2. Before that lease-period boundary block executes, the para's manager (or the `assigned_slots` pallet, or a `swap`) independently transitions the para's lifecycle away from `Parathread`/`Parachain` via `paras_registrar::swap`/`deregister`/`assigned_slots` calls.
3. At the lease-period boundary, `Slots::on_initialize` → `manage_lease_period_start` calls `T::Registrar::make_parachain(*para)` (or `make_parathread`), which now fails with `Error::<T>::NotParathread`/`NotParachain` because the lifecycle precondition no longer holds.
4. `debug_assert!(res.is_ok())` fails, panicking inside the mandatory `on_initialize` hook. On any debug-assertions-enabled node, block production halts at that block, denying service network-wide until manual intervention — a state-transition path analogous to, but broader in impact than, the original bricked-auction-house bug.

### Citations

**File:** polkadot/runtime/common/src/slots/mod.rs (L147-160)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		fn on_initialize(n: BlockNumberFor<T>) -> Weight {
			if let Some((lease_period, first_block)) = Self::lease_period_index(n) {
				// If we're beginning a new lease period then handle that.
				if first_block {
					return Self::manage_lease_period_start(lease_period);
				}
			}

			// We didn't return early above, so we didn't do anything.
			Weight::zero()
		}
	}
```

**File:** polkadot/runtime/common/src/slots/mod.rs (L285-301)
```rust
		parachains.sort();

		for para in parachains.iter() {
			if old_parachains.binary_search(para).is_err() {
				// incoming.
				let res = T::Registrar::make_parachain(*para);
				debug_assert!(res.is_ok());
			}
		}

		for para in old_parachains.iter() {
			if parachains.binary_search(para).is_err() {
				// outgoing.
				let res = T::Registrar::make_parathread(*para);
				debug_assert!(res.is_ok());
			}
		}
```

**File:** polkadot/runtime/common/src/paras_registrar/mod.rs (L521-544)
```rust
	// Upgrade a registered on-demand parachain into a lease holding parachain.
	fn make_parachain(id: ParaId) -> DispatchResult {
		// Para backend should think this is an on-demand parachain...
		ensure!(
			paras::Pallet::<T>::lifecycle(id) == Some(ParaLifecycle::Parathread),
			Error::<T>::NotParathread
		);
		polkadot_runtime_parachains::schedule_parathread_upgrade::<T>(id)
			.map_err(|_| Error::<T>::CannotUpgrade)?;

		Ok(())
	}

	// Downgrade a registered para into a parathread (on-demand parachain).
	fn make_parathread(id: ParaId) -> DispatchResult {
		// Para backend should think this is a parachain...
		ensure!(
			paras::Pallet::<T>::lifecycle(id) == Some(ParaLifecycle::Parachain),
			Error::<T>::NotParachain
		);
		polkadot_runtime_parachains::schedule_parachain_downgrade::<T>(id)
			.map_err(|_| Error::<T>::CannotDowngrade)?;
		Ok(())
	}
```

**File:** polkadot/runtime/common/src/assigned_slots/mod.rs (L575-584)
```rust
	/// Create a parachain slot lease based on given params.
	/// The function merely calls out to `Leaser::lease_out`.
	fn configure_slot_lease(
		para: ParaId,
		manager: T::AccountId,
		lease_period: LeasePeriodOf<T>,
		lease_duration: LeasePeriodOf<T>,
	) -> Result<(), LeaseError> {
		T::Leaser::lease_out(para, &manager, BalanceOf::<T>::zero(), lease_period, lease_duration)
	}
```
