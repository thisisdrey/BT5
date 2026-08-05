## Finding

### Title
Child-bounty curator can be instantly slashed via a stale parent `update_due` inherited without any per-child timer initialization - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`pallet-child-bounties::accept_curator` moves a child bounty into `ChildBountyStatus::Active { curator }` without recording any activity/due timestamp of its own [1](#0-0) . The permissionless "inactive curator" check performed later in `unassign_curator` does not use a child-specific timer at all — it borrows the **parent bounty's** `update_due` via `Self::ensure_bounty_active(parent_bounty_id)` [2](#0-1) . This is structurally the same defect as the Carapace `defaultStateManager` bug: a new sub-entity is admitted into an "Active" state while the state-machine field that is supposed to gate the next punitive transition (`update_due` / `lateTimestamp`) was never (re)initialized for that entity, so it can already be "due" the instant it is created.

### Finding Description
`pallet-bounties::accept_curator` sets `update_due = now + BountyUpdatePeriod` only for the **parent** bounty when its own curator accepts [3](#0-2) . That `update_due` is refreshed only through `extend_bounty_expiry`, and only by the parent curator's own action on the parent bounty — it is never touched by anything a child-bounty curator does.

When a **child bounty** curator calls `accept_curator`, the pallet transitions `ChildBountyStatus::CuratorProposed -> Active` and reserves a deposit, but stores no `update_due`/timestamp for the child bounty itself [4](#0-3) .

Later, `unassign_curator` for an `Active` child bounty allows **any signed account** (not just `RejectOrigin` or the parent curator) to slash the child curator's deposit if the *parent's* `update_due` has already elapsed:
```
Some(sender) => {
    let (parent_curator, update_due) = Self::ensure_bounty_active(parent_bounty_id)?;
    if sender == parent_curator || update_due < Self::treasury_block_number() {
        slash_curator(curator, &mut child_bounty.curator_deposit);
    } else {
        return Err(BountiesError::<T>::Premature.into());
    }
},
``` [2](#0-1) 

Because the parent bounty can have been `Active` for a long time (its `update_due` is a fixed block number set once when its own curator accepted, and only extended by explicit action of the *parent* curator, not automatically refreshed when child bounties/curators are created), a completely new child-bounty curator can accept their role at a moment when the parent's `update_due` has *already passed*. In that case the "grace period" the state machine is supposed to give a freshly-activated curator is zero: any unprivileged account can immediately call `unassign_curator` and permissionlessly slash the newly-reserved curator deposit, exactly analogous to the reported bug where a pool entering `Late` state with no `lateTimestamp` could be defaulted on the very next call.

This mirrors the two root causes from the external report:
1. A periodic/derived state value (`update_due`, analogous to `assessState`'s daily cadence) is not re-synchronized with the newly admitted entity (child bounty curator, analogous to a newly-added lending pool).
2. The status transition into `Active` does not separately initialize the entity's own timer, so a permissionless "inactivity" check downstream (`unassign_curator`, analogous to `_assessState`'s Late→Default) can fire the instant the entity exists.

### Impact Explanation
Any unprivileged account can force `slash_curator` to burn a newly-accepted child-bounty curator's deposit (`T::Currency::slash_reserved` + `T::OnSlash::on_unbalanced`) without the curator ever having a real chance to "update" the bounty, purely because the parent bounty's stale `update_due` was inherited. This is an unauthorized loss of held funds (curator deposit) triggered by a public entrypoint with no privileged actor involved, satisfying the "theft/unbacked loss of user funds via public entrypoint" impact class.

### Likelihood Explanation
This requires no attacker capital beyond gas: the attacker only needs to observe that a parent bounty's `update_due` has already elapsed (a value readable on-chain) and then call `unassign_curator` on any child bounty whose curator has just accepted under that parent. Parent bounties with long lifetimes or no recent `extend_bounty_expiry` calls, combined with new/late child-bounty curators, make this a realistic and cheap-to-trigger scenario, comparable in shape to the original report's "delay window vs. periodic external trigger" probability argument.

### Recommendation
Store a dedicated `update_due` (or `accepted_at`) timestamp on `ChildBounty` itself, set in `accept_curator` to `now + BountyUpdatePeriod` (mirroring `pallet-bounties::accept_curator`), and use that child-specific value — not the parent's — as the gating condition in `unassign_curator`'s permissionless-inactivity branch. Optionally also allow `extend_bounty_expiry`-equivalent functionality per child bounty.

### Proof of Concept
1. Parent bounty `B` becomes `Active` at block 0 with `BountyUpdatePeriod = 20`, so `update_due = 20`.
2. At block 25 (parent's `update_due` already elapsed, parent curator inactive but not yet unassigned), the parent curator adds a child bounty and proposes a new curator `C`.
3. At block 26, `C` calls `accept_curator` — child bounty becomes `Active { curator: C }` with no per-child timer.
4. At block 27, any account `X` (not `C`, not the parent curator, not `RejectOrigin`) calls `unassign_curator(parent_bounty_id, child_bounty_id)`.
5. Inside, `ensure_bounty_active` returns the parent's stale `update_due = 20 < 27`, so the `Active` branch's `Some(sender)` arm slashes `C`'s freshly reserved deposit immediately — one block after `C` accepted the role, with zero real "inactivity" period given.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L426-465)
```rust
		pub fn accept_curator(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;

			let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;
			// Mutate child-bounty.
			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					// Ensure child-bounty is in expected state.
					if let ChildBountyStatus::CuratorProposed { ref curator } = child_bounty.status
					{
						ensure!(signer == *curator, BountiesError::<T>::RequireCurator);

						// Reserve child-bounty curator deposit.
						let deposit = Self::calculate_curator_deposit(
							&parent_curator,
							curator,
							&child_bounty.fee,
						);

						T::Currency::reserve(curator, deposit)?;
						child_bounty.curator_deposit = deposit;

						child_bounty.status =
							ChildBountyStatus::Active { curator: curator.clone() };
						Ok(())
					} else {
						Err(BountiesError::<T>::UnexpectedStatus.into())
					}
				},
			)
		}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L565-580)
```rust
								Some(sender) => {
									let (parent_curator, update_due) =
										Self::ensure_bounty_active(parent_bounty_id)?;
									if sender == parent_curator ||
										update_due < Self::treasury_block_number()
									{
										// Slash the child-bounty curator if
										// + the call is made by the parent bounty curator.
										// + or the curator is inactive.
										slash_curator(curator, &mut child_bounty.curator_deposit);
									// Continue to change bounty status below.
									} else {
										// Curator has more time to give an update.
										return Err(BountiesError::<T>::Premature.into());
									}
								},
```

**File:** substrate/frame/bounties/src/lib.rs (L702-725)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::accept_curator())]
		pub fn accept_curator(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;

			Bounties::<T, I>::try_mutate_exists(bounty_id, |maybe_bounty| -> DispatchResult {
				let bounty = maybe_bounty.as_mut().ok_or(Error::<T, I>::InvalidIndex)?;

				match bounty.status {
					BountyStatus::CuratorProposed { ref curator } => {
						ensure!(signer == *curator, Error::<T, I>::RequireCurator);

						let deposit = Self::calculate_curator_deposit(&bounty.fee);
						T::Currency::reserve(curator, deposit)?;
						bounty.curator_deposit = deposit;

						let update_due = Self::treasury_block_number()
							.saturating_add(T::BountyUpdatePeriod::get());
						bounty.status =
							BountyStatus::Active { curator: curator.clone(), update_due };

```
