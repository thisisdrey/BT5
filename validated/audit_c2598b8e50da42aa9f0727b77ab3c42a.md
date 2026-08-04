### Title
Reward payout marked as claimed before the underlying currency transfer succeeds, permanently locking staking rewards when the transfer fails - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The external report's core invariant break is: *a payout function commits a currency transfer to a destination that can legitimately reject the transfer (via a rule tied to the receiving address), and the caller has no fallback, causing the payout to be irrecoverably lost/blocked.* In `pallet-staking-async`, `do_payout_stakers_by_page` reproduces the same broken invariant: it durably marks a reward page as claimed *before* the reward transfer to the payee is attempted, and if that transfer later fails (which routinely happens when the destination account doesn't exist and the reward is below the Existential Deposit), the failure is only logged — the claimed-state advance is never rolled back.

### Finding Description
In `do_payout_stakers_by_page`, the page is flagged as claimed via `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` at line 386, well before the actual payout is computed and dispatched: [1](#0-0) 

Later, when using the transfer-based (`use_dap_payout`) path, `Self::payout_from_provider` is invoked, which calls `Self::make_payout_from_provider` for the validator and each nominator: [2](#0-1) 

`make_payout_from_provider` performs a real `T::Currency::transfer` from the era reward pot to the resolved payee (`payout_account`). If this transfer fails — for example because the destination account (a `RewardDestination::Account(dest_account)` chosen by the staker via `set_payee`, or a stash/derived account) doesn't exist and the reward amount is below the chain's Existential Deposit — the error is only logged, and the function returns `None`: [3](#0-2) 

Because `Eras::<T>::set_rewards_as_claimed` already ran unconditionally before this point, and `do_payout_stakers_by_page` guards re-entry with `AlreadyClaimed`: [4](#0-3) 

there is no way to retry the payout for that `(era, stash, page)` — the reward for that nominator/validator is silently and permanently dropped from their perspective, while the funds simply remain stuck in the era reward pot instead of ever settling. This directly violates the required invariant that "payout state must only advance after ... execution and settlement succeed atomically" — here the claimed-state advances unconditionally, while settlement (the transfer) can independently fail.

By contrast, the legacy path `payout_legacy_mint` / `make_payout_legacy` uses `asset::mint_creating`, which cannot fail on a non-existent destination (it mints new supply directly into existence), so this specific failure mode is unique to the newer transfer-based reward-pot payout path added for `pallet-staking-async`.

### Impact Explanation
This is a public, unprivileged-triggerable payout path (`payout_stakers_by_page` / `payout_stakers`, called by anyone for any validator/era), and the loss condition can be reached by ordinary staking operations: a nominator whose reward destination account has never held a balance, combined with a reward amount smaller than the Existential Deposit (common for small nominators or granular per-page payouts), causes that nominator's share of the era reward to be permanently unclaimable while the funds are left orphaned in the reward pot. This matches the required impact category of "permanent user-fund ... lock" and a payout-settlement invariant violation, without requiring any malicious peer, validator, collator, or governance action — it is triggered by normal payout dispatch under ordinary account-state conditions.

### Likelihood Explanation
The condition is not contrived: rewards are split per-page across potentially many nominators via `Perbill` proportional division, so per-nominator amounts can easily be smaller than the Existential Deposit, especially for nominators with small stakes or pages with many nominators. Any staker can also set an arbitrary `RewardDestination::Account` via the standard `set_payee` extrinsic, including a fresh/unfunded account, without any validation that it can safely receive small transfers. No adversarial actor is needed — only a normal, permissionless call to `payout_stakers`/`payout_stakers_by_page` on an era/page that happens to produce a sub-ED transfer to an unfunded destination.

### Recommendation
Do not mark a reward page as claimed until each recipient's transfer has actually settled (or the amount is deliberately dropped by design, e.g. rounding dust). At minimum:
- Move `Eras::<T>::set_rewards_as_claimed` to occur only after the payouts for the page have been dispatched, or make claiming atomic-per-recipient rather than atomic-per-page.
- When `T::Currency::transfer` fails for a given payee in `make_payout_from_provider`, either retry with `Preservation::Expendable` semantics adjusted to guarantee success (e.g., top up to ED from the pot, mirroring `pallet-revive`'s `transfer` helper that funds the ED atomically before crediting `value`, see `substrate/frame/revive/src/exec.rs` lines 1711-1768), or explicitly redirect the undeliverable amount to a designated fallback/treasury account instead of silently dropping it, analogous to the recommended fix in the external report of routing failed transfers to `address(1)`.
- Add an explicit event/error surfaced to callers when a per-recipient transfer fails, so the failure is observable on-chain rather than only logged off-chain.

### Proof of Concept
1. Configure `DisableMintingGuard` so `use_dap_payout` is true for the era under test (enabling the transfer-based `payout_from_provider` path) as exercised in [5](#0-4) -style tests.
2. Bond a nominator and set `RewardDestination::Account(payee)` where `payee` is a brand-new account with zero balance, mirroring the pattern in `create_stash_and_dead_payee` used for worst-case payout testing: [6](#0-5) .
3. Ensure the nominator's computed `nominator_reward` for the target page (a `Perbill::from_rational` share of `total_nominator_payout`) rounds to less than the chain's `ExistentialDeposit`.
4. Call `payout_stakers_by_page` for that era/page. Observe:
   - `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` executes unconditionally (line 386).
   - `T::Currency::transfer(&staker_rewards_pot, &payout_account, amount, Preservation::Expendable)` in `make_payout_from_provider` fails because `payout_account` cannot be created below ED; the error is logged and `None` returned (lines 602-616), so no `Rewarded` event fires and no funds move.
5. Attempt to call `payout_stakers_by_page` again for the same `(era, stash, page)` — it returns `Error::<T>::AlreadyClaimed`, confirming the reward is permanently unclaimable while the funds remain stranded in the era reward pot.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-391)
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
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L577-617)
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

**File:** substrate/frame/staking-async/src/tests/validator_incentive.rs (L710-741)
```rust
#[test]
#[should_panic(expected = "Validator incentive liquid transfer failed")]
fn defensive_panic_on_transfer_failure() {
	ExtBuilder::default().build_and_execute(|| {
		let alice = 11; // validator

		// GIVEN: incentive enabled, validator has weight.
		setup_incentive_with_budget(45, 5);
		Session::roll_until_active_era(2);
		Eras::<Test>::reward_active_era(vec![(alice, 1), (21, 1)]);
		Session::roll_until_active_era(3);

		// WHEN: drain the incentive pot so transfer fails.
		let pot = <Test as Config>::RewardPots::pot_account(RewardPot::Era(
			2,
			RewardKind::ValidatorSelfStake,
		));
		let pot_balance = Balances::free_balance(&pot);
		if pot_balance > 0 {
			// Transfer everything out to account 999 to empty the pot.
			let _ = <Balances as frame_support::traits::fungible::Mutate<_>>::transfer(
				&pot,
				&999,
				pot_balance,
				frame_support::traits::tokens::Preservation::Expendable,
			);
		}

		// THEN: payout panics on defensive.
		make_all_reward_payment(2);
	});
}
```

**File:** substrate/frame/staking-async/src/testing_utils.rs (L123-139)
```rust
/// Create a stash and controller pair, where payouts go to a dead payee account. This is used to
/// test worst case payout scenarios.
pub fn create_stash_and_dead_payee<T: Config>(
	n: u32,
	balance_factor: u32,
) -> Result<(T::AccountId, T::AccountId), &'static str> {
	let staker = create_funded_user::<T>("stash", n, 0);
	// payee has no funds
	let payee = create_funded_user::<T>("payee", n, 0);
	let amount = asset::existential_deposit::<T>() * (balance_factor / 10).max(1).into();
	Staking::<T>::bond(
		RawOrigin::Signed(staker.clone()).into(),
		amount,
		RewardDestination::Account(payee),
	)?;
	Ok((staker.clone(), staker))
}
```
