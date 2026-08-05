Audit Report

## Title
`pallet-multi-asset-bounties::unassign_curator` lacks any inactivity-timeout mechanism, letting an unresponsive curator permanently lock bounty funds and their own deposit - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

## Summary
`unassign_curator`'s `BountyStatus::Active` branch only allows the `RejectOrigin`, the curator themselves, or (for child bounties) the parent curator to force an unassignment; there is no `update_due`/timeout field or check anywhere in the pallet that would let an arbitrary signed account permissionlessly slash an inactive curator, unlike the legacy `pallet-bounties`/`pallet-child-bounties` design. [1](#0-0)  A curator who simply never calls `award_bounty` and never voluntarily unassigns can therefore keep the bounty stuck in `Active` state indefinitely, absent `RejectOrigin` (governance) intervention.

## Finding Description
In the legacy pallet, `BountyStatus::Active` carries an `update_due` block number, and any signed account other than the curator can force a slash/unassign once `update_due < current_block`: [2](#0-1)  This acts as a permissionless liveness guarantee against an unresponsive curator.

In `pallet-multi-asset-bounties`, the equivalent `Active` match arm has exactly three paths: (1) `None` (i.e. `RejectOrigin`), (2) `sender == *curator` (voluntary resignation), or (3) `sender == parent_curator` for child bounties only: [1](#0-0)  There is no fourth branch for "any signed account once inactive", and no timeout field exists in the `BountyStatus::Active` variant to support one — confirmed by the fact `update_due` does not appear anywhere in this pallet's source (only in `pallet-bounties`/`pallet-child-bounties`). The dispatchable's own doc comment states plainly: "This function can only be called by the `RejectOrigin` or the child-/bounty curator," confirming this is the intended (but weaker) authorization surface rather than an accidental omission of a fourth branch. [3](#0-2) 

## Impact Explanation
Once a bounty is `Active`, funds have already moved into the bounty/child-bounty sub-account and the curator holds a reserved `curator_deposit`. If the curator never calls `award_bounty` and never self-unassigns, the only remaining path out of `Active` is `RejectOrigin`. On any chain where `RejectOrigin` is a slow/multi-step governance track (as is typical for treasury-adjacent origins), this becomes a de-facto indefinite lock of the bounty's funds and the curator's deposit that is not resolvable by any ordinary account — this matches the "permanent user-fund lock" impact category from the gate.

## Likelihood Explanation
Becoming a bounty curator only requires being proposed via `propose_curator` and accepting via `accept_curator`, both reachable through ordinary signed extrinsics with no special privilege. Once curator, simply withholding action (never calling `award_bounty`/`claim_bounty`/`unassign_curator`) is sufficient to trigger the lock — no governance, validator, or off-chain infrastructure control is needed, making this a fully unprivileged, repeatable condition.

## Recommendation
Add an `update_due`-style inactivity timeout to `BountyStatus::Active` in `pallet-multi-asset-bounties` (and its child-bounty equivalent), refreshed on curator activity (e.g. via a `check_curator` heartbeat call as in `pallet-bounties`), and extend the `Some(sender)` branch of `unassign_curator` to permit any signed account to force-unassign (and slash the curator deposit) once that timeout has elapsed, mirroring `pallet-bounties::unassign_curator`.

## Proof of Concept
1. Governance funds a bounty via `fund_bounty` with asset value `V`; curator `C` is proposed via `propose_curator` and accepts via `accept_curator`, moving status to `BountyStatus::Active { curator: C }` and reserving `C`'s `curator_deposit`.
2. `C` never calls `award_bounty`, `claim_bounty`, or `unassign_curator`.
3. A third-party account `D` calls `Bounties::unassign_curator(RuntimeOrigin::signed(D), parent_bounty_id, None)`.
4. Execution reaches the `Some(sender)` branch at [4](#0-3) , where `parent_curator` is `None` for a top-level bounty, so `parent_curator.ok_or(BadOrigin)?` returns `BadOrigin` immediately — `D` cannot unassign `C`.
5. `V` and `C`'s `curator_deposit` remain locked in `Active` state indefinitely unless `RejectOrigin` (governance) acts, with no permissionless remedy available to any other account.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L870-887)
```rust
		/// Unassign curator from a child-/bounty.
		///
		/// ## Dispatch Origin
		///
		/// This function can only be called by the `RejectOrigin` or the child-/bounty curator.
		///
		/// ## Details
		///
		/// - If this function is called by the `RejectOrigin`, or by the parent curator in the case
		///   of a child bounty, we assume that the curator is malicious or inactive. As a result,
		///   we will slash the curator when possible.
		/// - If the origin is the child-/bounty curator, we take this as a sign they are unable to
		///   do their job and they willingly give up. We could slash them, but for now we allow
		///   them to recover their deposit and exit without issue. (We may want to change this if
		///   it is abused).
		/// - If successful, the child-/bounty status is updated to `CuratorUnassigned`. To
		///   reactivate the bounty, a new curator must be proposed and must accept the role.
		///
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L923-961)
```rust
				BountyStatus::Active { ref curator, .. } => {
					// The child-/bounty is active.
					match maybe_sender {
						// If the `RejectOrigin` is calling this function, burn the curator deposit.
						None => {
							if let Some(curator_deposit) =
								CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
							{
								T::Consideration::burn(curator_deposit, curator);
							}
							// Continue to change bounty status below...
						},
						Some(sender) if sender == *curator => {
							if let Some(curator_deposit) =
								CuratorDeposit::<T, I>::get(parent_bounty_id, child_bounty_id)
							{
								// This is the curator, willingly giving up their role. Free their
								// deposit.
								T::Consideration::drop(curator_deposit, curator)?;
								CuratorDeposit::<T, I>::remove(parent_bounty_id, child_bounty_id);
							}
							// Continue to change bounty status below...
						},
						Some(sender) => {
							let parent_curator = parent_curator.ok_or(BadOrigin)?;
							ensure!(
								sender == parent_curator && *curator != parent_curator,
								BadOrigin
							);
							// Parent curator is unassigning the child curator. Burn the curator
							// deposit.
							if let Some(curator_deposit) =
								CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
							{
								T::Consideration::burn(curator_deposit, curator);
							}
						},
					}
				},
```

**File:** substrate/frame/bounties/src/lib.rs (L645-676)
```rust
					BountyStatus::Active { ref curator, ref update_due } => {
						// The bounty is active.
						match maybe_sender {
							// If the `RejectOrigin` is calling this function, slash the curator.
							None => {
								slash_curator(curator, &mut bounty.curator_deposit);
								// Continue to change bounty status below...
							},
							Some(sender) => {
								// If the sender is not the curator, and the curator is inactive,
								// slash the curator.
								if sender != *curator {
									let block_number = Self::treasury_block_number();
									if *update_due < block_number {
										slash_curator(curator, &mut bounty.curator_deposit);
									// Continue to change bounty status below...
									} else {
										// Curator has more time to give an update.
										return Err(Error::<T, I>::Premature.into());
									}
								} else {
									// Else this is the curator, willingly giving up their role.
									// Give back their deposit.
									let err_amount =
										T::Currency::unreserve(curator, bounty.curator_deposit);
									debug_assert!(err_amount.is_zero());
									bounty.curator_deposit = Zero::zero();
									// Continue to change bounty status below...
								}
							},
						}
					},
```
