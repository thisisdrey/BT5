Audit Report

## Title
Staking reward pots sweep their *entire* reducible balance into era payouts, letting arbitrary inflows be laundered into staker/validator rewards - ([File: substrate/frame/staking-async/src/reward.rs])

## Summary
`EraRewardManager::snapshot_era_rewards` reads the general staker-reward and validator-incentive pots' full `reducible_balance` and transfers that entire amount into the era-specific pot, rather than transferring only the amount actually minted by `pallet-dap`'s `mint_and_distribute` for that period. Since these pot accounts are ordinary `AccountId`s deterministically derived via `PalletId::into_sub_account_truncating`/`Seed<StakingPotsPalletId>`, any signed account can pre-fund them with a plain `balances::transfer_keep_alive`, and those funds will be swept into the era pot and paid out to validators/nominators as if they were legitimate rewards.

## Finding Description
The general pots are computed from `T::RewardPots::pot_account(RewardPot::General(RewardKind::StakerRewards|ValidatorSelfStake))`, itself `S::get().into_sub_account_truncating(...)` [1](#0-0) , so both pots are publicly derivable off-chain with no privileged input.

`pallet_dap::mint_and_distribute` mints inflation into these pot accounts based on `BudgetAllocation` percentages [2](#0-1) , but no per-era/per-pot storage tracks how much was minted this way.

At era rotation, `EraRewardManager::snapshot_era_rewards` reads the pot's *entire* `reducible_balance` (`Preservation::Preserve`, i.e. everything above ED) and unconditionally transfers that full amount to the era-specific pot: [3](#0-2) . There is no check comparing this balance against a tracked "amount actually minted this era" figure — the balance itself is the source of truth. The resulting `actual_staker`/`actual_incentive` values (which include any extraneous transfer) become the `EraRewardAllocation` that callers persist as `ErasValidatorReward`/`ErasValidatorIncentiveBudget` for the era, and are later transferred out during payout via `make_payout_from_provider`/`transfer_validator_incentive`, which move funds strictly from the era pot to stash/payee accounts with no re-validation against the DAP-minted amount [4](#0-3) [5](#0-4) .

Because nothing gates the pot balance against a minted-amount ledger, a plain signed account calling `Balances::transfer_keep_alive` (or `transfer_allow_death`) to the general staker pot (or directly to the current era pot, equally derivable via `RewardPot::Era(era, kind)`) before the era-boundary snapshot or before `payout_stakers` claims that era's exposure results in those donated funds being distributed to validators/nominators as staking reward, with no filter, no origin check, and no reconciliation against `ErasValidatorReward`.

## Impact Explanation
This corrupts the on-chain economic accounting invariant that reward pots must distribute exactly the funds intentionally allocated by DAP's inflation curve to the rightful stakers, no more and no less. Foreign inflows sent to the deterministic pot address for any unrelated reason (accidental transfer, misconfigured `OnUnbalanced` wiring, dust reaping, or a deliberate transfer) are irreversibly reallocated to whichever validators/nominators hold exposure that era, desynchronizing `ErasValidatorReward`/`ErasValidatorIncentiveBudget` (and the `era_reward_allocation` view function [6](#0-5) ) from the true minted amount. This matches the required "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for staking/bridge reward payouts, and represents a real fund-misdirection/accounting-corruption bug reachable via unprivileged public extrinsics (`balances::transfer_keep_alive`).

## Likelihood Explanation
The pot accounts are deterministic PalletId-seeded accounts, computable off-chain by anyone with no special access, and standard `pallet_balances` transfer extrinsics are unrestricted (no filter prevents transferring to these accounts) [1](#0-0) . Era boundaries and payout windows are public on-chain state, so timing a transfer before `snapshot_era_rewards` runs is trivial and fully repeatable every era without any validator, collator, or governance cooperation.

## Recommendation
Track the amount actually credited by `mint_and_distribute` per pot/era in dedicated storage (e.g. incremented at mint time), and have `snapshot_era_rewards` transfer only up to that tracked entitlement rather than `reducible_balance`. Any balance beyond the tracked entitlement should be treated as foreign/unexpected and redirected (e.g., to the DAP buffer or burned) instead of being distributed to stakers.

## Proof of Concept
1. Off-chain, compute `general_staker_pot = Seed::<StakingPotsPalletId>::pot_account(RewardPot::General(RewardKind::StakerRewards))` (mirrors `<Test as Config>::RewardPots::pot_account(...)` used in `substrate/frame/staking-async/src/tests/validator_incentive.rs`).
2. From any signed account, submit `Balances::transfer_keep_alive(general_staker_pot, X)`.
3. Let the era rotate; `EraRewardManager::snapshot_era_rewards` (`substrate/frame/staking-async/src/reward.rs:88-125`) reads `reducible_balance(&general_staker_pot, ...)`, which now includes `X`, and moves the full amount (inflation + `X`) to the era pot.
4. `payout_stakers` (via `make_payout_from_provider`, `substrate/frame/staking-async/src/pallet/impls.rs:578-630`) distributes the inflated pot, including `X`, to validators/nominators proportional to exposure, while `ErasValidatorReward` for that era reflects the polluted (inflated) figure rather than the true DAP-minted amount — a unit/integration test asserting `Balances::free_balance(&era_pot) == pre_transfer_snapshot + X` after the donation and before payout would demonstrate the corruption.

### Citations

**File:** substrate/frame/staking-async/src/lib.rs (L641-657)
```rust
impl<AccountId, S> PotAccountProvider<AccountId> for Seed<S>
where
	AccountId: codec::FullCodec,
	S: Get<frame_support::PalletId>,
{
	fn pot_account(pot: RewardPot) -> AccountId {
		use sp_runtime::traits::AccountIdConversion;
		// Era pots are addressed by slot (`era % POT_POOL_SIZE`), not by the
		// raw era index, so a fixed pool of accounts rotates instead of
		// growing per era.
		let normalized = match pot {
			RewardPot::Era(era, kind) => RewardPot::Era(pot_slot(era), kind),
			other => other,
		};
		S::get().into_sub_account_truncating(normalized)
	}
}
```

**File:** substrate/frame/dap/src/lib.rs (L411-446)
```rust
		pub(crate) fn mint_and_distribute(elapsed: u64) -> BalanceOf<T> {
			let total_issuance = T::Currency::total_issuance();
			let issuance = T::IssuanceCurve::issue(total_issuance, elapsed);

			if issuance.is_zero() {
				return BalanceOf::<T>::zero();
			}

			let budget = BudgetAllocation::<T>::get();
			if budget.is_empty() {
				// TODO: Add defensive! panic once budget is always configured.
				log::warn!(
					target: LOG_TARGET,
					"BudgetAllocation is empty — no issuance will be distributed"
				);
				return BalanceOf::<T>::zero();
			}
			let recipients = T::BudgetRecipients::recipients();
			let mut total_minted = BalanceOf::<T>::zero();

			let buffer = Self::buffer_account();
			for (key, account) in &recipients {
				let perbill = budget.get(key).copied().unwrap_or(Perbill::zero());
				let amount = perbill.mul_floor(issuance);
				if !amount.is_zero() {
					if let Err(_) = T::Currency::mint_into(account, amount) {
						Self::deposit_event(Event::Unexpected(UnexpectedKind::MintFailed));
						defensive!("Issuance mint should not fail");
					} else {
						total_minted = total_minted.saturating_add(amount);
						if *account == buffer {
							Self::deactivate_buffer_funds(amount);
						}
					}
				}
			}
```

**File:** substrate/frame/staking-async/src/reward.rs (L97-125)
```rust
		// Leave ED in the general pots to keep them alive.
		let staker_balance = T::Currency::reducible_balance(
			&general_staker_pot,
			Preservation::Preserve,
			Fortitude::Polite,
		);
		let incentive_balance = T::Currency::reducible_balance(
			&general_incentive_pot,
			Preservation::Preserve,
			Fortitude::Polite,
		);

		let actual_staker = if !staker_balance.is_zero() {
			match T::Currency::transfer(
				&general_staker_pot,
				&staker_era_pot,
				staker_balance,
				Preservation::Preserve,
			) {
				Ok(_) => staker_balance,
				Err(e) => {
					log!(error, "Era {:?}: staker reward transfer failed: {:?}", era, e);
					defensive!("Failed to transfer staker rewards to era pot");
					Zero::zero()
				},
			}
		} else {
			Zero::zero()
		};
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L598-616)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L776-794)
```rust
		let incentive_pot = T::RewardPots::pot_account(crate::RewardPot::Era(
			era,
			crate::RewardKind::ValidatorSelfStake,
		));

		match T::Currency::transfer(
			&incentive_pot,
			&payout_account,
			amount,
			Preservation::Expendable,
		) {
			Ok(_) => {
				Self::deposit_event(Event::<T>::ValidatorIncentivePaid {
					era,
					validator_stash: stash.clone(),
					dest,
					amount,
				});
			},
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L3290-3300)
```rust
		/// Per-era reward allocation (staker rewards + validator incentive budget).
		///
		/// Both fields are zero for eras created in legacy minting mode.
		pub fn era_reward_allocation(
			era: EraIndex,
		) -> crate::reward::EraRewardAllocation<BalanceOf<T>> {
			crate::reward::EraRewardAllocation {
				staker_rewards: ErasValidatorReward::<T>::get(era).unwrap_or_else(Zero::zero),
				validator_incentive: ErasValidatorIncentiveBudget::<T>::get(era),
			}
		}
```
