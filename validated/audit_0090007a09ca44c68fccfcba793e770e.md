This confirms `pallet-bounties` has `reclaim_bounty_funds` (implemented per PR 11045) [1](#0-0) , while `pallet-child-bounties` has no equivalent reclaim function anywhere in its source [2](#0-1) . Both `claim_child_bounty` and `impl_close_child_bounty` transfer the child bounty account's balance and then remove the `ChildBounties` storage entry, but this only occurs as part of the normal call flow at the exact moment of removal — there is no separate sweep for funds that arrive *after* removal [3](#0-2) [4](#0-3) .

Audit Report

## Title
Stranded funds in closed child-bounty accounts have no reclaim path - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`pallet-child-bounties` derives a per-child-bounty sub-account via `child_bounty_account_id` that holds the child bounty's funds during its lifecycle. Once a child bounty transitions to a terminal state via `claim_child_bounty` or `close_child_bounty` (`impl_close_child_bounty`), its `ChildBounties` storage entry is removed and no dispatchable in the pallet ever references that account again, so any balance sent to it afterward (accidental transfer, dust, delayed settlement) is permanently stranded with no sweep mechanism, unlike the sibling `pallet-bounties`, which added `reclaim_bounty_funds` for exactly this scenario.

## Finding Description
`Self::child_bounty_account_id(parent_bounty_id, child_bounty_id)` is a deterministic `AccountIdConversion`-derived sub-account [5](#0-4) . Funds are moved into it from the parent bounty account in `add_child_bounty` [6](#0-5) . The two terminal paths, `claim_child_bounty` and `impl_close_child_bounty`, both read `T::Currency::free_balance(&child_bounty_account)` at call time and transfer out exactly that amount before setting `*maybe_child_bounty = None`, removing the `ChildBounties` entry [7](#0-6) [4](#0-3) . Once `ChildBounties::<T>::get(parent_bounty_id, child_bounty_id)` is `None`, every other dispatchable in the pallet (`add_child_bounty`, `propose_curator`, `accept_curator`, `unassign_curator`, `award_child_bounty`, `claim_child_bounty`, `close_child_bounty`) either operates on a different `(parent_bounty_id, child_bounty_id)` key or immediately fails with `InvalidIndex`/`ParentBountyNotActive` because `try_mutate_exists` finds nothing [8](#0-7) . There is no analog to `pallet_bounties::reclaim_bounty_funds`, which is explicitly gated on `!Bounties::<T, I>::contains_key(bounty_id)` and permissionlessly sweeps a closed bounty account's residual balance to the treasury [9](#0-8) . Grepping the entire `substrate/frame/child-bounties` directory for `reclaim` returns zero matches, confirming no such function exists for child bounties despite the identical architectural pattern.

## Impact Explanation
Any native currency sent to a child-bounty sub-account after its `ChildBounties` entry is removed becomes permanently unrecoverable — not by treasury, governance, curator, or beneficiary — since no code path in the pallet ever addresses that account again. This is a permanent fund lock, matching the "permanent user-fund or bridge-state lock" category in the impact gate.

## Likelihood Explanation
No privileged capability is required. Any unprivileged account can observe a child bounty transitioning to closed (event `Claimed` or `Canceled`) and then send an ordinary `balances::transfer` to the deterministically-derived `child_bounty_account_id(parent_bounty_id, child_bounty_id)`. This is trivially and repeatably reachable through normal pallet usage plus one extra transfer, exactly mirroring the `reclaim_bounty_funds_works_after_accidental_refund` scenario that was fixed for the parent pallet but left unaddressed for child bounties.

## Recommendation
Add a permissionless `reclaim_child_bounty_funds` extrinsic to `pallet-child-bounties`, gated on `!ChildBounties::<T>::contains_key(parent_bounty_id, child_bounty_id)`, that sweeps any residual balance from `Self::child_bounty_account_id(parent_bounty_id, child_bounty_id)` back to the parent bounty account or treasury, mirroring `pallet_bounties::reclaim_bounty_funds`.

## Proof of Concept
1. Create a parent bounty, fund and activate it, then add a child bounty via `add_child_bounty`, which transfers `value` into `child_bounty_account_id(parent_bounty_id, child_bounty_id)`.
2. Drive the child bounty to completion via `propose_curator` → `accept_curator` → `award_child_bounty` → (after delay) `claim_child_bounty`, which pays out the full balance and removes the `ChildBounties` entry.
3. Send an arbitrary balance directly to `ChildBounties::child_bounty_account_id(parent_bounty_id, child_bounty_id)` via a plain `balances::transfer`.
4. Observe there is no extant call referencing this account (`grep -r reclaim substrate/frame/child-bounties` returns nothing), so the balance remains permanently stranded, unlike the equivalent scenario in `pallet-bounties`, which is resolved by `reclaim_bounty_funds`.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L1048-1090)
```rust
		///
		/// Permissionless. Moves all remaining assets from a closed bounty's account back to the
		/// treasury in a single call. Which assets are swept depends on the `TransferAllAssets`
		/// configuration.
		///
		/// The call is free if funds were reclaimed and paid otherwise, so no-op calls cannot be
		/// used to grief the network. Emits `BountyFundsReclaimed` on success.
		///
		/// ## Complexity
		/// - O(A) where A is the number of relevant assets configured in `TransferAllAssets`.
		#[pallet::call_index(11)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::reclaim_bounty_funds())]
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;

			// Free only if something moved, otherwise paid to prevent griefing.
			if !transferred {
				return Ok(Pays::Yes.into());
			}

			Self::deposit_event(Event::<T, I>::BountyFundsReclaimed { bounty_id });

			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L254-815)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Add a new child-bounty.
		///
		/// The dispatch origin for this call must be the curator of parent
		/// bounty and the parent bounty must be in "active" state.
		///
		/// Child-bounty gets added successfully & fund gets transferred from
		/// parent bounty to child-bounty account, if parent bounty has enough
		/// funds, else the call fails.
		///
		/// Upper bound to maximum number of active  child bounties that can be
		/// added are managed via runtime trait config
		/// [`Config::MaxActiveChildBountyCount`].
		///
		/// If the call is success, the status of child-bounty is updated to
		/// "Added".
		///
		/// - `parent_bounty_id`: Index of parent bounty for which child-bounty is being added.
		/// - `value`: Value for executing the proposal.
		/// - `description`: Text description for the child-bounty.
		#[pallet::call_index(0)]
		#[pallet::weight(<T as Config>::WeightInfo::add_child_bounty(description.len() as u32))]
		pub fn add_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] value: BalanceOf<T>,
			description: Vec<u8>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;

			// Verify the arguments.
			let bounded_description =
				description.try_into().map_err(|_| BountiesError::<T>::ReasonTooBig)?;
			ensure!(value >= T::ChildBountyValueMinimum::get(), BountiesError::<T>::InvalidValue);
			ensure!(
				ParentChildBounties::<T>::get(parent_bounty_id) <
					T::MaxActiveChildBountyCount::get(),
				Error::<T>::TooManyChildBounties,
			);

			let (curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;
			ensure!(signer == curator, BountiesError::<T>::RequireCurator);

			// Read parent bounty account info.
			let parent_bounty_account =
				pallet_bounties::Pallet::<T>::bounty_account_id(parent_bounty_id);

			// Ensure parent bounty has enough balance after adding child-bounty.
			let bounty_balance = T::Currency::free_balance(&parent_bounty_account);
			let new_bounty_balance = bounty_balance
				.checked_sub(&value)
				.ok_or(Error::<T>::InsufficientBountyBalance)?;
			T::Currency::ensure_can_withdraw(
				&parent_bounty_account,
				value,
				WithdrawReasons::TRANSFER,
				new_bounty_balance,
			)?;

			// Get child-bounty ID.
			let child_bounty_id = ParentTotalChildBounties::<T>::get(parent_bounty_id);
			let child_bounty_account =
				Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);

			// Transfer funds from parent bounty to child-bounty.
			T::Currency::transfer(&parent_bounty_account, &child_bounty_account, value, KeepAlive)?;

			// Increment the active child-bounty count.
			ParentChildBounties::<T>::mutate(parent_bounty_id, |count| count.saturating_inc());
			ParentTotalChildBounties::<T>::insert(
				parent_bounty_id,
				child_bounty_id.saturating_add(1),
			);

			// Create child-bounty instance.
			Self::create_child_bounty(
				parent_bounty_id,
				child_bounty_id,
				value,
				bounded_description,
			);
			Ok(())
		}

		/// Propose curator for funded child-bounty.
		///
		/// The dispatch origin for this call must be curator of parent bounty.
		///
		/// Parent bounty must be in active state, for this child-bounty call to
		/// work.
		///
		/// Child-bounty must be in "Added" state, for processing the call. And
		/// state of child-bounty is moved to "CuratorProposed" on successful
		/// call completion.
		///
		/// - `parent_bounty_id`: Index of parent bounty.
		/// - `child_bounty_id`: Index of child bounty.
		/// - `curator`: Address of child-bounty curator.
		/// - `fee`: payment fee to child-bounty curator for execution.
		#[pallet::call_index(1)]
		#[pallet::weight(<T as Config>::WeightInfo::propose_curator())]
		pub fn propose_curator(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
			curator: AccountIdLookupOf<T>,
			#[pallet::compact] fee: BalanceOf<T>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let child_bounty_curator = T::Lookup::lookup(curator)?;

			let (curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;
			ensure!(signer == curator, BountiesError::<T>::RequireCurator);

			// Mutate the child-bounty instance.
			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					// Ensure child-bounty is in expected state.
					ensure!(
						child_bounty.status == ChildBountyStatus::Added,
						BountiesError::<T>::UnexpectedStatus,
					);

					// Ensure child-bounty curator fee is less than child-bounty value.
					ensure!(fee < child_bounty.value, BountiesError::<T>::InvalidFee);

					// Add child-bounty curator fee to the cumulative sum. To be
					// subtracted from the parent bounty curator when claiming
					// bounty.
					ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| {
						*value = value.saturating_add(fee)
					});

					// Update the child-bounty curator fee.
					child_bounty.fee = fee;

					// Update the child-bounty state.
					child_bounty.status =
						ChildBountyStatus::CuratorProposed { curator: child_bounty_curator };

					Ok(())
				},
			)
		}

		/// Accept the curator role for the child-bounty.
		///
		/// The dispatch origin for this call must be the curator of this
		/// child-bounty.
		///
		/// A deposit will be reserved from the curator and refund upon
		/// successful payout or cancellation.
		///
		/// Fee for curator is deducted from curator fee of parent bounty.
		///
		/// Parent bounty must be in active state, for this child-bounty call to
		/// work.
		///
		/// Child-bounty must be in "CuratorProposed" state, for processing the
		/// call. And state of child-bounty is moved to "Active" on successful
		/// call completion.
		///
		/// - `parent_bounty_id`: Index of parent bounty.
		/// - `child_bounty_id`: Index of child bounty.
		#[pallet::call_index(2)]
		#[pallet::weight(<T as Config>::WeightInfo::accept_curator())]
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

		/// Unassign curator from a child-bounty.
		///
		/// The dispatch origin for this call can be either `RejectOrigin`, or
		/// the curator of the parent bounty, or any signed origin.
		///
		/// For the origin other than T::RejectOrigin and the child-bounty
		/// curator, parent bounty must be in active state, for this call to
		/// work. We allow child-bounty curator and T::RejectOrigin to execute
		/// this call irrespective of the parent bounty state.
		///
		/// If this function is called by the `RejectOrigin` or the
		/// parent bounty curator, we assume that the child-bounty curator is
		/// malicious or inactive. As a result, child-bounty curator deposit is
		/// slashed.
		///
		/// If the origin is the child-bounty curator, we take this as a sign
		/// that they are unable to do their job, and are willingly giving up.
		/// We could slash the deposit, but for now we allow them to unreserve
		/// their deposit and exit without issue. (We may want to change this if
		/// it is abused.)
		///
		/// Finally, the origin can be anyone iff the child-bounty curator is
		/// "inactive". Expiry update due of parent bounty is used to estimate
		/// inactive state of child-bounty curator.
		///
		/// This allows anyone in the community to call out that a child-bounty
		/// curator is not doing their due diligence, and we should pick a new
		/// one. In this case the child-bounty curator deposit is slashed.
		///
		/// State of child-bounty is moved to Added state on successful call
		/// completion.
		///
		/// - `parent_bounty_id`: Index of parent bounty.
		/// - `child_bounty_id`: Index of child bounty.
		#[pallet::call_index(3)]
		#[pallet::weight(<T as Config>::WeightInfo::unassign_curator())]
		pub fn unassign_curator(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			let maybe_sender = ensure_signed(origin.clone())
				.map(Some)
				.or_else(|_| T::RejectOrigin::ensure_origin(origin).map(|_| None))?;

			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					let slash_curator =
						|curator: &T::AccountId, curator_deposit: &mut BalanceOf<T>| {
							let imbalance =
								T::Currency::slash_reserved(curator, *curator_deposit).0;
							T::OnSlash::on_unbalanced(imbalance);
							*curator_deposit = Zero::zero();
						};

					match child_bounty.status {
						ChildBountyStatus::Added => {
							// No curator to unassign at this point.
							return Err(BountiesError::<T>::UnexpectedStatus.into());
						},
						ChildBountyStatus::CuratorProposed { ref curator } => {
							// A child-bounty curator has been proposed, but not accepted yet.
							// Either `RejectOrigin`, parent bounty curator or the proposed
							// child-bounty curator can unassign the child-bounty curator.
							ensure!(
								maybe_sender.map_or(true, |sender| {
									sender == *curator ||
										Self::ensure_bounty_active(parent_bounty_id)
											.map_or(false, |(parent_curator, _)| {
												sender == parent_curator
											})
								}),
								BadOrigin
							);
							// Continue to change bounty status below.
						},
						ChildBountyStatus::Active { ref curator } => {
							// The child-bounty is active.
							match maybe_sender {
								// If the `RejectOrigin` is calling this function, slash the curator
								// deposit.
								None => {
									slash_curator(curator, &mut child_bounty.curator_deposit);
									// Continue to change child-bounty status below.
								},
								Some(sender) if sender == *curator => {
									// This is the child-bounty curator, willingly giving up their
									// role. Give back their deposit.
									T::Currency::unreserve(curator, child_bounty.curator_deposit);
									// Reset curator deposit.
									child_bounty.curator_deposit = Zero::zero();
									// Continue to change bounty status below.
								},
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
							}
						},
						ChildBountyStatus::PendingPayout { ref curator, .. } => {
							let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;
							ensure!(
								maybe_sender.map_or(true, |sender| parent_curator == sender),
								BadOrigin,
							);
							slash_curator(curator, &mut child_bounty.curator_deposit);
							// Continue to change child-bounty status below.
						},
					};
					// Move the child-bounty state to Added.
					child_bounty.status = ChildBountyStatus::Added;
					Ok(())
				},
			)
		}

		/// Award child-bounty to a beneficiary.
		///
		/// The beneficiary will be able to claim the funds after a delay.
		///
		/// The dispatch origin for this call must be the parent curator or
		/// curator of this child-bounty.
		///
		/// Parent bounty must be in active state, for this child-bounty call to
		/// work.
		///
		/// Child-bounty must be in active state, for processing the call. And
		/// state of child-bounty is moved to "PendingPayout" on successful call
		/// completion.
		///
		/// - `parent_bounty_id`: Index of parent bounty.
		/// - `child_bounty_id`: Index of child bounty.
		/// - `beneficiary`: Beneficiary account.
		#[pallet::call_index(4)]
		#[pallet::weight(<T as Config>::WeightInfo::award_child_bounty())]
		pub fn award_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
			beneficiary: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

			// Ensure parent bounty exists, and is active.
			let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;

			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					// Ensure child-bounty is in active state.
					if let ChildBountyStatus::Active { ref curator } = child_bounty.status {
						ensure!(
							signer == *curator || signer == parent_curator,
							BountiesError::<T>::RequireCurator,
						);
						// Move the child-bounty state to pending payout.
						child_bounty.status = ChildBountyStatus::PendingPayout {
							curator: signer,
							beneficiary: beneficiary.clone(),
							unlock_at: Self::treasury_block_number() +
								T::BountyDepositPayoutDelay::get(),
						};
						Ok(())
					} else {
						Err(BountiesError::<T>::UnexpectedStatus.into())
					}
				},
			)?;

			// Trigger the event Awarded.
			Self::deposit_event(Event::<T>::Awarded {
				index: parent_bounty_id,
				child_index: child_bounty_id,
				beneficiary,
			});

			Ok(())
		}

		/// Claim the payout from an awarded child-bounty after payout delay.
		///
		/// The dispatch origin for this call may be any signed origin.
		///
		/// Call works independent of parent bounty state, No need for parent
		/// bounty to be in active state.
		///
		/// The Beneficiary is paid out with agreed bounty value. Curator fee is
		/// paid & curator deposit is unreserved.
		///
		/// Child-bounty must be in "PendingPayout" state, for processing the
		/// call. And instance of child-bounty is removed from the state on
		/// successful call completion.
		///
		/// - `parent_bounty_id`: Index of parent bounty.
		/// - `child_bounty_id`: Index of child bounty.
		#[pallet::call_index(5)]
		#[pallet::weight(<T as Config>::WeightInfo::claim_child_bounty())]
		pub fn claim_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			ensure_signed(origin)?;

			// Ensure child-bounty is in expected state.
			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					if let ChildBountyStatus::PendingPayout {
						ref curator,
						ref beneficiary,
						ref unlock_at,
					} = child_bounty.status
					{
						// Ensure block number is elapsed for processing the
						// claim.
						ensure!(
							Self::treasury_block_number() >= *unlock_at,
							BountiesError::<T>::Premature,
						);

						// Make curator fee payment.
						let child_bounty_account =
							Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
						let balance = T::Currency::free_balance(&child_bounty_account);
						let curator_fee = child_bounty.fee.min(balance);
						let payout = balance.saturating_sub(curator_fee);

						// Unreserve the curator deposit. Should not fail
						// because the deposit is always reserved when curator is
						// assigned.
						let _ = T::Currency::unreserve(curator, child_bounty.curator_deposit);

						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());

						// Trigger the Claimed event.
						Self::deposit_event(Event::<T>::Claimed {
							index: parent_bounty_id,
							child_index: child_bounty_id,
							payout,
							beneficiary: beneficiary.clone(),
						});

						// Update the active child-bounty tracking count.
						ParentChildBounties::<T>::mutate(parent_bounty_id, |count| {
							count.saturating_dec()
						});

						// Remove the child-bounty description.
						ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

						// Remove the child-bounty instance from the state.
						*maybe_child_bounty = None;

						Ok(())
					} else {
						Err(BountiesError::<T>::UnexpectedStatus.into())
					}
				},
			)
		}

		/// Cancel a proposed or active child-bounty. Child-bounty account funds
		/// are transferred to parent bounty account. The child-bounty curator
		/// deposit may be unreserved if possible.
		///
		/// The dispatch origin for this call must be either parent curator or
		/// `T::RejectOrigin`.
		///
		/// If the state of child-bounty is `Active`, curator deposit is
		/// unreserved.
		///
		/// If the state of child-bounty is `PendingPayout`, call fails &
		/// returns `PendingPayout` error.
		///
		/// For the origin other than T::RejectOrigin, parent bounty must be in
		/// active state, for this child-bounty call to work. For origin
		/// T::RejectOrigin execution is forced.
		///
		/// Instance of child-bounty is removed from the state on successful
		/// call completion.
		///
		/// - `parent_bounty_id`: Index of parent bounty.
		/// - `child_bounty_id`: Index of child bounty.
		#[pallet::call_index(6)]
		#[pallet::weight(<T as Config>::WeightInfo::close_child_bounty_added()
			.max(<T as Config>::WeightInfo::close_child_bounty_active()))]
		pub fn close_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			let maybe_sender = ensure_signed(origin.clone())
				.map(Some)
				.or_else(|_| T::RejectOrigin::ensure_origin(origin).map(|_| None))?;

			// Ensure parent bounty exist, get parent curator.
			let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;

			ensure!(maybe_sender.map_or(true, |sender| parent_curator == sender), BadOrigin);

			Self::impl_close_child_bounty(parent_bounty_id, child_bounty_id)?;
			Ok(())
		}
	}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L853-862)
```rust
	/// The account ID of a child-bounty account.
	pub fn child_bounty_account_id(
		parent_bounty_id: BountyIndex,
		child_bounty_id: BountyIndex,
	) -> T::AccountId {
		// This function is taken from the parent (bounties) pallet, but the
		// prefix is changed to have different AccountId when the index of
		// parent and child is same.
		T::PalletId::get().into_sub_account_truncating(("cb", parent_bounty_id, child_bounty_id))
	}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L882-903)
```rust
	fn ensure_bounty_active(
		bounty_id: BountyIndex,
	) -> Result<(T::AccountId, BlockNumberFor<T>), DispatchError> {
		let parent_bounty = pallet_bounties::Bounties::<T>::get(bounty_id)
			.ok_or(BountiesError::<T>::InvalidIndex)?;
		if let BountyStatus::Active { curator, update_due } = parent_bounty.get_status() {
			Ok((curator, update_due))
		} else {
			Err(Error::<T>::ParentBountyNotActive.into())
		}
	}

	fn impl_close_child_bounty(
		parent_bounty_id: BountyIndex,
		child_bounty_id: BountyIndex,
	) -> DispatchResult {
		ChildBounties::<T>::try_mutate_exists(
			parent_bounty_id,
			child_bounty_id,
			|maybe_child_bounty| -> DispatchResult {
				let child_bounty =
					maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;
```

**File:** substrate/frame/child-bounties/src/lib.rs (L934-951)
```rust
				// Transfer fund from child-bounty to parent bounty.
				let parent_bounty_account =
					pallet_bounties::Pallet::<T>::bounty_account_id(parent_bounty_id);
				let child_bounty_account =
					Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
				let balance = T::Currency::free_balance(&child_bounty_account);
				let transfer_result = T::Currency::transfer(
					&child_bounty_account,
					&parent_bounty_account,
					balance,
					AllowDeath,
				); // Should not fail; child bounty account gets this balance during creation.
				debug_assert!(transfer_result.is_ok());

				// Remove the child-bounty description.
				ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

				*maybe_child_bounty = None;
```
