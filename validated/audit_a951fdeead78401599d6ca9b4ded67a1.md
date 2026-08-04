### Title
Staking reward payout marks era/page as claimed before verifying the value transfer to the reward destination succeeded, permanently burning rewards on transfer failure - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The external report's core defect is: an unprivileged user chooses a payout recipient, the protocol commits to a "settled" state before the money actually moves, the transfer to that recipient then fails, and the failure is swallowed rather than rolling back the settlement state — permanently breaking the liquidation invariant. The same broken pattern exists in `pallet-staking-async`'s reward payout flow: the reward page is marked `claimed` in storage before the actual `Currency::transfer` to the (user-chosen) `RewardDestination` account is attempted, and if that transfer fails the function only logs an error and returns `None` — the dispatchable still returns `Ok(())`, so the "claimed" marker is never rolled back.

### Finding Description
In `do_payout_stakers_by_page`, the page is committed as claimed strictly before any payout money movement occurs: [1](#0-0) 

Later, actual payouts are performed via `make_payout_from_provider`, which resolves the payout account from the unprivileged, user-set `RewardDestination` and performs a real `T::Currency::transfer` (not a mint): [2](#0-1) 

Critically, if that transfer errors, the function does not propagate the error to abort the extrinsic — it only logs and returns `None`: [3](#0-2) 

The `RewardDestination` is fully attacker-controlled via the public, unprivileged `set_payee` call: [4](#0-3) 

This is structurally identical to the Witch `auctioneer` bug: a user-supplied destination address is accepted without any validation that a payment to it can succeed, the "action is complete" state (`Attempted auction` in Witch / `set_rewards_as_claimed` here) is committed unconditionally, and the actual value-transfer step that depends on that address is allowed to fail silently afterward with no automatic remediation. `payout_stakers`/`payout_stakers_by_page` is a permissionless, public dispatchable — "Any account can call this function, even if it is not one of the stakers" — so any account can trigger the payout, and once the transfer to a "poisoned" `RewardDestination::Account` fails, `Eras::<T>::is_rewards_claimed(era, &stash, page)` is already `true`, permanently blocking any retry of that page/era's reward for the stash and its nominators.

### Impact Explanation
This breaks the "payout state must only advance after ... settlement succeeds atomically" invariant explicitly called out for this scope. A validator or nominator can set an unpayable `RewardDestination::Account` (e.g. an address engineered to reject the deposit through `frame_system`/`fungible` deposit constraints such as consumer/provider limits, or any custom `Currency`/asset backend in a parachain runtime where deposits can fail), causing the era/page reward to be marked claimed while the underlying value is never delivered. Because `ClaimedRewards`/paged-claim tracking is keyed per `(era, stash, page)` and checked via `AlreadyClaimed`, there is no built-in path to re-attempt payment for that specific page once marked — the reward for every nominator sharing that exposure page is permanently lost, a direct "permanent user-fund lock" outcome from an unprivileged, public entrypoint.

### Likelihood Explanation
`set_payee` and `payout_stakers_by_page`/`payout_stakers` are both ordinary signed, permissionless calls with no admin/governance/privileged actor involved — any staker can set their own reward destination, and any account (including a hostile third party) can be the one to trigger the payout that gets stuck. The bug does not require a malicious validator/collator/relayer/prover, only a signed extrinsic from a standard account, matching the "public dispatch wrapper" and "reward payouts" attack surfaces named in scope.

### Recommendation
Do not commit `set_rewards_as_claimed`/mark the page as claimed until the payout transfer (or the full set of payouts for that page) has actually succeeded, or alternatively: on individual payout failure, keep the page/era in a retryable state (mirroring the `PaymentState::Failed`/`check_status`/retry pattern already used in `pallet-treasury`'s async payout flow) rather than unconditionally advancing `ClaimedRewards`. At minimum, propagate transfer failures from `make_payout_from_provider` so the whole page-payout extrinsic reverts (including the claimed-state write) instead of silently discarding the error.

### Proof of Concept
1. Attacker (or any nominator/validator) stashes funds normally and calls `Staking::set_payee(origin, RewardDestination::Account(poisoned_account))` where `poisoned_account` is engineered so that a real `Currency::transfer` deposit to it fails (e.g., an account that cannot accept a deposit under the runtime's configured `Currency`/asset implementation — consumer-limit exhaustion, a non-mintable/holdable asset backend used via `T::Currency`, etc.).
2. An era completes; rewards accrue and `Eras::<T>::get_stakers_reward(era)` is populated.
3. Any signed account calls `payout_stakers_by_page(origin, validator_stash, era, page)`.
4. `do_payout_stakers_by_page` executes `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` unconditionally at line 386, then proceeds to call `make_payout_from_provider` for each payee in the page.
5. For the poisoned payee, `T::Currency::transfer` errors; the error is logged and `None` returned; the dispatchable still returns `Ok(...)`.
6. `Eras::<T>::is_rewards_claimed(era, &stash, page)` is now permanently `true` for this page — no further call to `payout_stakers`/`payout_stakers_by_page` for that `(era, stash, page)` can ever succeed (`AlreadyClaimed`), and the reward funds for every account in that exposure page are permanently unrecoverable.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-393)
```rust
		if Eras::<T>::is_rewards_claimed(era, &stash, page) {
			return Err(Error::<T>::AlreadyClaimed
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)));
		}

		Eras::<T>::set_rewards_as_claimed(era, &stash, page);

		let exposure = Eras::<T>::get_paged_exposure(era, &stash, page).ok_or_else(|| {
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;

		// Input data seems good, no errors allowed after this point
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L563-575)
```rust
	/// Determine the payout account from a reward destination.
	fn payout_account_for_dest(
		stash: &T::AccountId,
		dest: &RewardDestination<T::AccountId>,
	) -> Option<T::AccountId> {
		match dest {
			RewardDestination::Stash | RewardDestination::Staked => Some(stash.clone()),
			RewardDestination::Account(ref dest_account) => Some(dest_account.clone()),
			RewardDestination::None => None,
			#[allow(deprecated)]
			RewardDestination::Controller => Self::bonded(stash),
		}
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L577-616)
```rust
	/// Make a payment to a staker from an era reward pot (transfer, not mint).
	fn make_payout_from_provider(
		era: EraIndex,
		stash: &T::AccountId,
		amount: BalanceOf<T>,
	) -> Option<(BalanceOf<T>, RewardDestination<T::AccountId>)> {
		if amount.is_zero() {
			return None;
		}

		let dest = match Self::payee(Stash(stash.clone())) {
			Some(d) => d,
			None => {
				Self::deposit_event(Event::<T>::Unexpected(UnexpectedKind::MissingPayee {
					era,
					stash: stash.clone(),
				}));
				return None;
			},
		};

		let payout_account = Self::payout_account_for_dest(stash, &dest)?;

		let staker_rewards_pot =
			T::RewardPots::pot_account(RewardPot::Era(era, RewardKind::StakerRewards));
		if let Err(e) = T::Currency::transfer(
			&staker_rewards_pot,
			&payout_account,
			amount,
			Preservation::Expendable,
		) {
			log!(
				error,
				"Failed to transfer reward from pot for era {:?}, stash {:?}: {:?}",
				era,
				stash,
				e
			);
			return None;
		}
```
