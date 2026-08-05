### Title
Permissionless `import_member` Timer-Reset Allows Anyone to Indefinitely Block Core-Fellowship Auto-Demotion - (File: `substrate/frame/core-fellowship/src/lib.rs`)

### Summary
`pallet-core-fellowship` enforces rank accountability via `bump`, which lets *anyone* auto-demote a member once their `demotion_period` has elapsed since `last_proof`/`last_promotion`. The pallet also exposes `import_member`, callable by *any signed origin* on *any* ranked collective member, which unconditionally resets that member's `last_proof` to the current block and `last_promotion` to zero. There is no restriction preventing repeated calls on an already-tracked member, so the same "reset the clock before the fallback/enforcement mechanism can act" pattern from the Linea `renounceRole`/`setFallbackOperator` report reappears here: the enforcement path (`bump`) and the free-form reset path (`import_member`) are both public, and the reset path always wins if invoked before `bump`.

### Finding Description
- `bump` demotes a member once `now >= demotion_block` (computed from `last_proof` + `demotion_period`), and can be called by anyone: [1](#0-0) 
- `import_member` is explicitly documented as callable "by anyone on any collective member - including the sender," and resets `last_proof` to now / `last_promotion` to zero, "thereby delaying any automatic demotion": [2](#0-1) 
- The call is fee-free (`Pays::No`) on success, so the reset can be repeated at zero cost by any account, including a throwaway/unrelated address, ahead of every `bump` attempt: [3](#0-2) 
- The module-level design doc states demotion is meant to be enforced by "anyone" calling `bump` once `demotion_period` elapses, i.e., `bump` is the intended corrective/enforcement mechanism analogous to the Linea `fallbackOperator` recovery path: [4](#0-3) 
- Existing tests confirm that any call that touches `last_proof` (e.g. `approve`) postpones the demotion window and makes a pending `bump` fail with `NothingDoing`: [5](#0-4) 

The broken invariant mirrors the external report exactly: a public, unprivileged, zero-cost action (`import_member`) can reset the same timer that a public enforcement action (`bump`) depends on, and there is no guard preventing the reset from being issued by an account unrelated to the member, or from being repeated indefinitely. Because `bump`'s `demotion_block` check only compares against `last_proof`, and `import_member` unconditionally overwrites `last_proof`/`last_promotion` with no rank- or track-status gate that would block re-import of an already-tracked member, the demotion clock can be perpetually refreshed by a colluding or self-interested third party racing every pending `bump`.

### Impact Explanation
This defeats the pallet's core accountability guarantee: an inactive or misbehaving ranked member (e.g., holding `PromoteOrigin`/`ApproveOrigin`-adjacent influence in a Fellowship/Collectives governance track) can never be auto-demoted as long as any signed account (including a throwaway account controlled by the member themselves) submits `import_member` before each `bump` window closes. This is a "runtime bug that compromises intended behavior" of a governance-accountability mechanism — the demotion/offboarding safety valve becomes permanently disable-able by an unprivileged actor, without needing governance, admin, or validator collusion.

### Likelihood Explanation
High: the call is public, free (`Pays::No`), requires no special origin beyond `ensure_signed`, and needs to be triggered only once per `demotion_period` — a simple recurring extrinsic (a bot/cron script) suffices to keep any target permanently un-demotable. No race against block production is even required since `import_member` just needs to land in any block before the corresponding `bump` succeeds.

### Recommendation
Restrict `import_member`'s effect on already-tracked members: either (a) require that the target is not already present in `Member` storage (matching the deprecated `import`'s documented precondition of "ranked, but not tracked"), or (b) restrict who may invoke `import_member` on a given `who` to `who` themselves or a privileged origin, so the clock-reset primitive cannot be wielded by unrelated third parties to shield a member from auto-demotion.

### Proof of Concept
1. Member `M` is ranked with `demotion_period = D`; `last_proof` is at block `B`.
2. As block `B + D` approaches, an unrelated signed account `A` (or `M` itself via an alt account) submits `import_member(M)`.
3. `do_import` resets `M.last_proof = now`, `M.last_promotion = 0`, extending the demotion window to `now + D`.
4. Any later `bump(M)` call in the interim fails with `Error::NothingDoing` because `now < demotion_block`.
5. Repeat step 2 every `< D` blocks indefinitely — `M` is never auto-demoted regardless of continued inactivity, exactly mirroring the Linea report's renounce-reset cycle against the fallback operator.

### Citations

**File:** substrate/frame/core-fellowship/src/lib.rs (L30-39)
```rust
//! - Some time later but before rank 1's `demotion_period` elapses, candidate calls
//!   `submit_evidence` with evidence of their efforts to apply for approval to stay at rank 1.
//! - An `ApproveOrigin` of at least rank 1 calls `approve` on the candidate to avoid imminent
//!   demotion and keep it at rank 1.
//! - These last two steps continue until the candidate is ready to apply for a promotion, at which
//!   point the previous two steps are repeated with a higher rank.
//! - If the member fails to get an approval within the `demotion_period` then anyone may call
//!   `bump` to demote the candidate by one rank.
//! - If a candidate fails to be promoted to a member within the `offboard_timeout` period, then
//!   anyone may call `bump` to remove the account's candidacy.
```

**File:** substrate/frame/core-fellowship/src/lib.rs (L380-399)
```rust

			// Ensure enough time has passed.
			let now = T::BlockNumberProvider::current_block_number();
			if now >= demotion_block {
				T::Members::demote(&who)?;
				let maybe_to_rank = T::Members::rank_of(&who);
				Self::dispose_evidence(who.clone(), rank, maybe_to_rank);
				let event = if let Some(to_rank) = maybe_to_rank {
					member.last_proof = now;
					Member::<T, I>::insert(&who, &member);
					Event::<T, I>::Demoted { who, to_rank }
				} else {
					Member::<T, I>::remove(&who);
					Event::<T, I>::Offboarded { who }
				};
				Self::deposit_event(event);
				return Ok(Pays::No.into());
			}

			Err(Error::<T, I>::NothingDoing.into())
```

**File:** substrate/frame/core-fellowship/src/lib.rs (L631-658)
```rust
		pub fn import(origin: OriginFor<T>) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;
			Self::do_import(who)?;

			Ok(Pays::No.into()) // Successful imports are free
		}

		/// Introduce an already-ranked individual of the collective into this pallet.
		///
		/// The rank may still be zero. Can be called by anyone on any collective member - including
		/// the sender.
		///
		/// This resets `last_proof` to the current block and `last_promotion` will be set to zero,
		/// thereby delaying any automatic demotion but allowing immediate promotion.
		///
		/// - `origin`: A signed origin of a ranked, but not tracked, account.
		/// - `who`: The account ID of the collective member to be inducted.
		#[pallet::weight(T::WeightInfo::set_partial_params())]
		#[pallet::call_index(11)]
		pub fn import_member(
			origin: OriginFor<T>,
			who: T::AccountId,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			Self::do_import(who)?;

			Ok(Pays::No.into()) // Successful imports are free
		}
```

**File:** substrate/frame/core-fellowship/src/tests/unit.rs (L499-510)
```rust
#[test]
fn proof_postpones_auto_demote() {
	new_test_ext().execute_with(|| {
		set_rank(10, 5);
		assert_ok!(CoreFellowship::import(signed(10)));

		run_to(11);
		assert_ok!(CoreFellowship::approve(signed(5), 10, 5));
		assert_eq!(next_demotion(10), 21);
		assert_noop!(CoreFellowship::bump(signed(0), 10), Error::<Test>::NothingDoing);
	});
}
```
