Confirmed: `pallet-multi-asset-bounties`'s `BountyStatus::Active` variant carries **no** `update_due`/timeout field at all (unlike `pallet-bounties`/`pallet-child-bounties`, which track `update_due` and expose a permissionless, inactivity-based `unassign_curator` path gated by `Error::Premature`). This confirms the analog is real and locally provable.

### Title
Curator can permanently withhold a funded bounty's value with no permissionless inactivity remedy - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

### Summary
The external report's core broken invariant is: a low-cost actor (validator operator) can, by simply accepting a role tied to custody of a much larger pool of funds, freeze that larger amount indefinitely, because no permissionless "blame/timeout" path exists to release it — only the actor themself (or, in Stake pool, nobody) can trigger release. In `pallet-multi-asset-bounties`, once a curator posts a small `CuratorDeposit` and the bounty transitions to `BountyStatus::Active { curator, .. }`, the (potentially much larger) `value` of the bounty stays locked in the bounty's derived account until the curator calls `award_bounty`. The pallet's `unassign_curator` ( [1](#0-0) ) only allows: (a) the curator themself to voluntarily step down, or (b) the `RejectOrigin`, or (c) for a **child** bounty, the *parent curator*. For a **top-level (parent) bounty** in `Active` status, the `Some(sender)` branch requires `parent_curator.ok_or(BadOrigin)?` — which is always `None` for a parent bounty — so any non-curator, non-`RejectOrigin` signed account is rejected with `BadOrigin`, unconditionally, forever, with no inactivity timer at all.

### Finding Description
Compare with the legacy `pallet-bounties::unassign_curator`, which explicitly supports a third, fully permissionless path: any signed account can slash and unassign an inactive curator once `block_number > update_due` ( [2](#0-1) ), and identically in `pallet-child-bounties::unassign_curator` ( [3](#0-2) ). This "anyone can blame an inactive curator after a due date" mechanism is exactly the fix the external report recommends for the PROPOSED-validator lock: introduce a permissionless way to blame an actor who accepted responsibility for funds but never completes the follow-up action.

`pallet-multi-asset-bounties::BountyStatus::Active` carries only `{ curator, payment_status? }` — there is no `update_due` field, no `Premature` error, and no code path that lets a third party act on curator inactivity for a **parent** bounty: [1](#0-0) 

Once a bounty is `Active`, its `value` (in `asset_kind`) sits parked in the bounty's derived account (analogous to the pool's 31 ETH parked under `rks.secured`), released only via `award_bounty`, which requires `signer == *curator` ( [4](#0-3) ). If the curator simply never calls `award_bounty` and never voluntarily unassigns, the only remaining path is `RejectOrigin` (privileged/governance) calling `unassign_curator` — there is no permissionless "curator went dark" recourse at all for a parent bounty, whereas the legacy pallets it superseded explicitly provided one.

### Impact Explanation
A curator who accepts a bounty (posting only a small `CuratorDeposit`) can indefinitely park the full `Bounty::value` — which can be arbitrarily larger than the curator's own deposit, mirroring the 1 ETH-locks-31-ETH asymmetry in the external report — with zero community-level recourse. Absent active `RejectOrigin`/governance intervention, treasury/bounty funds are effectively frozen with no on-chain, permissionless path to reassign a new curator or reclaim the funds, degrading treasury liveness and value custody guarantees.

### Likelihood Explanation
Likelihood is moderate: it requires no malicious peer, validator, or governance actor — simply an ordinary, unprivileged curator who accepts a bounty and then goes inactive (deliberately or not). This is a realistic, low-effort scenario since accepting a bounty only costs the `CuratorDeposit`, and the pallet places no time bound on how long a curator can sit in the `Active` state before award.

### Recommendation
Add an `update_due`-style expiry field to `BountyStatus::Active` (and/or `PayoutAttempted`) for parent bounties in `pallet-multi-asset-bounties`, an `extend_bounty_expiry`-equivalent call for the curator to periodically prove liveness, and extend `unassign_curator`'s `Some(sender)` branch to permit any signed account to unassign (and slash) the curator once the due date has elapsed — mirroring the existing `pallet-bounties`/`pallet-child-bounties` inactivity-blame design.

### Proof of Concept
1. Governance funds a bounty via `fund_bounty` with a large `value` and proposes a curator; the curator calls `accept_curator`, posting `CuratorDeposit`, moving `status` to `BountyStatus::Active { curator, .. }` ( [5](#0-4)  shows the `Active` state being reached).
2. The curator never calls `award_bounty` and never calls `unassign_curator` themself.
3. Any other signed account attempts `unassign_curator(origin, parent_bounty_id, None)`; because `parent_curator` resolves to `None` for a parent bounty, the call unconditionally returns `BadOrigin` regardless of how much time has elapsed — unlike the analogous `expire_and_unassign` test in `pallet-bounties`, which succeeds after `update_due` passes ( [6](#0-5) ).
4. The bounty's `value` remains parked in the bounty account indefinitely unless `RejectOrigin` (governance) intervenes.

**Uncertainty note:** I could not fully confirm within the available tool budget whether `RejectOrigin` in production runtimes is configured to be reachable quickly/cheaply (e.g., via a fast-track committee) versus only through full governance referenda, which affects how severe the practical "permanent lock" window is; a Devin session with full repo/runtime-config access would be needed to verify this and to check whether any other unexplored code path (e.g., a bounty-level `close_bounty` timeout) mitigates the issue.

### Citations

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

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1002-1024)
```rust
		pub fn award_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			child_bounty_id: Option<BountyIndex>,
			beneficiary: BeneficiaryLookupOf<T, I>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::BeneficiaryLookup::lookup(beneficiary)?;

			let (asset_kind, value, _, status, _) =
				Self::get_bounty_details(parent_bounty_id, child_bounty_id)?;

			if child_bounty_id.is_none() {
				ensure!(
					ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0,
					Error::<T, I>::HasActiveChildBounty
				);
			}

			let BountyStatus::Active { ref curator } = status else {
				return Err(Error::<T, I>::UnexpectedStatus.into());
			};
			ensure!(signer == *curator, Error::<T, I>::RequireCurator);
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

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L1628-1642)
```rust
		// Given: parent bounty status is `Active` and sender is the curator
		let s = create_active_parent_bounty();

		// When: sender is the curator
		assert_ok!(Bounties::unassign_curator(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			None
		));

		// Then
		assert_eq!(
			Balances::free_balance(&s.curator),
			Balances::minimum_balance() + s.curator_deposit
		); // not burned
```

**File:** substrate/frame/bounties/src/tests.rs (L1008-1049)
```rust
#[test]
fn expire_and_unassign() {
	ExtBuilder::default().build_and_execute(|| {
		Balances::make_free_balance_be(&Treasury::account_id(), 101);
		assert_ok!(Bounties::propose_bounty(RuntimeOrigin::signed(0), 50, b"12345".to_vec()));

		assert_ok!(Bounties::approve_bounty(RuntimeOrigin::root(), 0));

		go_to_block(2);

		assert_ok!(Bounties::propose_curator(RuntimeOrigin::root(), 0, 1, 10));
		assert_ok!(Bounties::accept_curator(RuntimeOrigin::signed(1), 0));

		assert_eq!(Balances::free_balance(1), 93);
		assert_eq!(Balances::reserved_balance(1), 5);

		go_to_block(22);

		assert_noop!(
			Bounties::unassign_curator(RuntimeOrigin::signed(0), 0),
			Error::<Test>::Premature
		);

		go_to_block(23);

		assert_ok!(Bounties::unassign_curator(RuntimeOrigin::signed(0), 0));

		assert_eq!(
			pallet_bounties::Bounties::<Test>::get(0).unwrap(),
			Bounty {
				proposer: 0,
				fee: 10,
				curator_deposit: 0,
				value: 50,
				bond: 85,
				status: BountyStatus::Funded,
			}
		);

		assert_eq!(Balances::free_balance(1), 93);
		assert_eq!(Balances::reserved_balance(1), 0); // slashed
	});
```
