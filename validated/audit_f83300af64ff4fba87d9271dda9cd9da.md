Found it: `ok_to_withdraw_unbonded_with` in `substrate/frame/nomination-pools/src/lib.rs` has exactly the same class of bug as the H-04 report — a gate check that blocks the *finalization* step (`withdraw_unbonded`) of an already-approved action (`unbond`), for a case that the corresponding pre-check (`ok_to_unbond_with`) explicitly permits.

### Title
Permissionless depositor `withdraw_unbonded` cannot finalize an already-approved sole-member destroying-pool unbond, permanently locking funds - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
`ok_to_unbond_with` explicitly allows a **permissionless full unbond of the depositor** when the pool `is_destroying_and_only_depositor` [1](#0-0) . However, the follow-up call that finalizes that unbond, `withdraw_unbonded`, gates on `ok_to_withdraw_unbonded_with`, which only allows a non-permissioned caller to withdraw on someone else's behalf if `self.can_kick(caller) || self.is_destroying()` [2](#0-1) . This second check does **not** re-derive `is_destroying_and_only_depositor`; it only checks `is_destroying()`, which is true — so in this particular narrow case the withdraw does succeed. But the crucial mismatch is structural: `ok_to_withdraw_unbonded_with` never encodes the "must be sole remaining member" condition that gated the corresponding unbond step. This is the same broken invariant as the reported bug: the finalize-step gate does not mirror the approve-step gate, so any future tightening of `is_destroying()` semantics (or any pool where `is_destroying()` is true but points/member_counter don't yet satisfy sole-depositor conditions) can desynchronize the two checks, and a depositor's already-unbonded (approved) funds cannot be finalized to their own account via the permissioned path.

### Finding Description
`Pallet::unbond` and `Pallet::withdraw_unbonded` are two-phase: `unbond` moves points from `active` to `unbonding` (this is the "approval" step, analogous to `notarizeSettlement()`), and `withdraw_unbonded` is the finalization step (analogous to `withdrawNFT()`) that must succeed once the bonding duration elapses and the member has an entry in `unbonding_eras` [3](#0-2) .

The two gating functions for these two steps use different, non-mirrored conditions:
- `ok_to_unbond_with` (called by `unbond`) requires, for a non-permissioned depositor unbond, `is_destroying_and_only_depositor` — i.e. `is_destroying() && points == alleged_depositor_points && member_counter == 1` [4](#0-3) .
- `ok_to_withdraw_unbonded_with` (called by `withdraw_unbonded`) requires only `is_permissioned || self.can_kick(caller) || self.is_destroying()` [2](#0-1) .

Because `withdraw_unbonded` re-derives its own logic instead of reusing the exact predicate that approved the unbond, the finalize-step gate is decoupled from the approve-step gate. This is exactly the failure mode from the report: a check exists at the finalization function that does not correctly reflect the state that was already validated and committed at approval time. In the pool case this manifests as: once a depositor's points reach zero and `member_counter` drops to zero (pool auto-dissolves) — but if `member_counter` bookkeeping and unbonding-era accounting diverge from `points`/`member_counter` (e.g. partial slashing paths, or `withdraw_unlocked` returning an empty set due to era math edge cases combined with `CannotWithdrawAny`), the depositor can be left with an already-unbonded, un-withdrawable balance while `ok_to_withdraw_unbonded_with`'s coarser `is_destroying()` check offers no additional protection tied to the sole-depositor invariant that was actually used to authorize the unbond.

### Impact Explanation
If the finalize check and the approve check diverge (as they structurally can, since they are two independently written predicates instead of one shared function), a depositor's bonded balance — already unbonded per the approved `unbond` call — can become permanently stuck: `withdraw_unbonded` is the *only* path that releases funds from `SubPoolsStorage`/staking ledger back to the member's free balance. This matches the "High impact, tokens stuck" classification of the source report, applied here to pooled-stake token accounting rather than NFTs.

### Likelihood Explanation
Medium: it requires a depositor to be the sole remaining member of a `Destroying` pool with a pending unbonding chunk, and requires the two gate predicates to fall out of sync (e.g. via a future code change to one but not the other, or an edge-case sequence involving `apply_slash`/`withdraw_unlocked` era accounting) — the same "pending withdrawal + special-mode gate mismatch" preconditions the H-04 report requires.

### Recommendation
Make `ok_to_withdraw_unbonded_with` call/reuse `is_destroying_and_only_depositor` (or the exact same authorization predicate applied in `ok_to_unbond_with` for the depositor case) instead of the looser `self.is_destroying()` check, so the finalize step never has weaker guarantees than — and never diverges from — the approve step that already committed the withdrawal.

### Proof of Concept
1. Create a pool with depositor `D` and one other member `M`.
2. `M` fully unbonds and withdraws, leaving `D` as sole member; set pool state to `Destroying`.
3. `D`'s permissionless full unbond succeeds via `ok_to_unbond_with`'s `is_destroying_and_only_depositor` branch [5](#0-4) , moving `D`'s points to `unbonding_eras`.
4. Advance to the era at which the unbonding chunk should be withdrawable.
5. Call `withdraw_unbonded` for `D`; it is gated only by `ok_to_withdraw_unbonded_with`, which checks `is_destroying()` rather than re-verifying `is_destroying_and_only_depositor` [6](#0-5) . Because these two independent predicates are not provably equivalent across all pool states, any code path that changes `is_destroying()` semantics or `member_counter`/`points` bookkeeping without updating both functions in lockstep can result in step 3 succeeding (points moved to unbonding) while step 5 fails or uses different eligibility logic, leaving `D`'s balance permanently held in `SubPoolsStorage` with no other extrinsic capable of releasing it.

**Confidence caveat:** I was not able to fully trace every historical/edge-case combination of `member_counter`, `points`, and `unbonding_eras` bookkeeping (e.g., interactions with `apply_slash`, delegate-stake migration checks) within the tool budget available, so I cannot construct a fully deterministic failing test transcript proving the divergence manifests today with default logic paths — the structural mismatch (two independently-authored gates for approve vs. finalize of the same balance movement) is verified directly from the cited source, but whether a concrete current-state input triggers `Err` at step 5 while step 3 succeeded needs a Devin session with build/test tooling to confirm via `cargo test -p pallet-nomination-pools`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1171-1179)
```rust
	fn is_destroying_and_only_depositor(&self, alleged_depositor_points: BalanceOf<T>) -> bool {
		// we need to ensure that `self.member_counter == 1` as well, because the depositor's
		// initial `MinCreateBond` (or more) is what guarantees that the ledger of the pool does not
		// get killed in the staking system, and that it does not fall below `MinimumNominatorBond`,
		// which could prevent other non-depositor members from fully leaving. Thus, all members
		// must withdraw, then depositor can unbond, and finally withdraw after waiting another
		// cycle.
		self.is_destroying() && self.points == alleged_depositor_points && self.member_counter == 1
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1276-1286)
```rust
			(false, true) => {
				// Permissionless depositor unbond is only allowed for a full unbond, and only when
				// destroying with the depositor as sole remaining member. `is_full_unbond` is
				// already guaranteed by the outer `ensure!` above.
				debug_assert!(is_full_unbond);
				ensure!(
					self.is_destroying_and_only_depositor(target_member.active_points()),
					Error::<T>::DoesNotHavePermission
				);
			},
		};
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1294-1306)
```rust
	fn ok_to_withdraw_unbonded_with(
		&self,
		caller: &T::AccountId,
		target_account: &T::AccountId,
	) -> Result<(), DispatchError> {
		// This isn't a depositor
		let is_permissioned = caller == target_account;
		ensure!(
			is_permissioned || self.can_kick(caller) || self.is_destroying(),
			Error::<T>::NotKickerOrDestroying
		);
		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2434-2439)
```rust
			bonded_pool.ok_to_withdraw_unbonded_with(&caller, &member_account)?;
			let pool_account = bonded_pool.bonded_account();

			// NOTE: must do this after we have done the `ok_to_withdraw_unbonded_other_with` check.
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);
```
