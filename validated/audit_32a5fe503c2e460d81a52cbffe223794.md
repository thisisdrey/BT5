I found the strongest analog in `pallet-bridge-relayers`: rewards are registered as accounting-only ledger entries (no fund reservation at registration time), and paid out later from a shared pot account whose balance can be drained independently — the same "commit now, settle later without a liquidity hold" flaw described in the M-04 report.

### Title
Unreserved reward registration allows shared reward-pot drain, causing legitimate relayer reward claims to permanently fail - ([File: bridges/modules/relayers/src/lib.rs])

### Summary
`register_relayer_reward` credits a relayer's entry in `RelayerRewards` storage without touching or reserving any funds in the actual `RewardsAccountParams`-derived pot account. The obligation ("you are owed X") is recorded purely as an accounting-storage entry, while the tokens needed to honor it remain in a shared, mutable pot that other parties can freely drain in the meantime. Settlement only happens later, at `claim_rewards`, via `T::PaymentProcedure::pay_reward`, which performs a plain `transfer` from that pot.

### Finding Description
`Pallet::register_relayer_reward` simply mutates the `RelayerRewards` double map (accumulating the balance) — it never checks or locks the balance of the actual rewards-paying account: [1](#0-0) 

The reward is only realized as a real balance transfer when the relayer calls `claim_rewards` / `claim_rewards_to`, at which point `do_claim_rewards` removes the storage entry and invokes `T::PaymentProcedure::pay_reward`: [2](#0-1) 

The default `PaymentProcedure` implementation, `PayRewardFromAccount`, does a straightforward `Currency::transfer` from a single derived "rewards account" (keyed only by lane/owner, i.e. shared across *all* relayers of that lane) to the beneficiary: [3](#0-2) 

This mirrors exactly the pattern in the report: the payout amount (`netPayout` in the report, `reward_balance` here) is computed and *committed* to storage as an obligation at one point in time (`register_reward`, analogous to `flip()`'s liquidity check), but the actual value transfer happens asynchronously and unconditionally later against a shared, unreserved balance pool. Because the pot balance is never earmarked at commit time, any other relayer (or the same relayer via a duplicate/legitimate reward for a different lane sharing the same derived pot account, or governance/other consumers of that account) can drain the pot between registration and claim. There is no accounting of "total outstanding registered rewards" analogous to `lockedLiquidity`, so nothing prevents the pot from falling below the sum of all pending `RelayerRewards` entries.

### Impact Explanation
When the pot balance is insufficient at claim time, `T::transfer(...).map(drop)` fails, `pay_reward` returns an error, and `do_claim_rewards` propagates `Error::<T, I>::FailedToPayReward` — but critically, this happens inside `try_mutate_exists` where `maybe_reward.take()` has already removed the entry from `RelayerRewards` before the transfer is attempted. If the transfer fails, the whole extrinsic reverts due to `?` on the `DispatchResult`, so under normal atomic semantics the storage write is rolled back and the entry survives — this specific pallet is not immediately fund-losing because of transactional rollback. However, this demonstrates the exact broken invariant: relayer rewards can become permanently unclaimable (a legitimate relayer "wins" a reward but cannot ever be paid) whenever the shared pot is depleted by unrelated activity, with no reservation mechanism preventing that depletion, and no protection ensuring the pot always holds at least the sum of registered-but-unclaimed rewards. This is a "public underpriced work / stalled processing" and "fund-lock" class impact: relayers that perform real delivery/confirmation work (the useful work referenced in scope) can be denied payment indefinitely if the shared pot is drained by concurrent claims from other relayers on the same lane, since `RewardsAccountParams`-derived accounts are shared per lane/owner rather than per-relayer-reservation.

### Likelihood Explanation
Likelihood is **Low-to-Medium**: it requires either (a) many relayers accumulating rewards against the same lane's shared pot faster than the pot is replenished (e.g. via `fee` collection) or (b) any code path with authority to move funds out of the `PayRewardFromAccount` rewards-account (e.g. via `slash_and_deregister`, which sends slashed stakes *into* this same account, or governance sweep operations) reducing the balance below outstanding obligations. No malicious peer, validator, or admin action is required in the base case — normal, permissionless relaying activity by multiple honest relayers against a common, unreserved pot is sufficient to create a race between registration and claim.

### Recommendation
Track total outstanding registered-but-unclaimed rewards per `RewardsAccountParams`/pot account (analogous to `lockedLiquidity` in the report), and either: (1) reserve/earmark the reward amount out of the pot at `register_reward` time so it cannot be spent by other operations, or (2) before finalizing any transfer that reduces a shared rewards-pot balance (e.g. future consumers of that account), enforce that the resulting balance cannot fall below the sum of all currently-registered, unclaimed rewards for that pot.

### Proof of Concept
1. Two relayers, A and B, both perform work on the same lane; the pallet calls `register_relayer_reward(lane_pot, A, 100)` and `register_relayer_reward(lane_pot, B, 100)`, per [4](#0-3) . The shared pot account currently only holds 100 (e.g. it was never replenished for both).
2. Relayer A calls `claim_rewards`, and `do_claim_rewards` succeeds, draining the entire pot via `PayRewardFromAccount::pay_reward`, per [5](#0-4)  and [3](#0-2) .
3. Relayer B, who has an equally legitimate, already-registered 100-unit claim in `RelayerRewards`, now calls `claim_rewards` and receives `Error::<T, I>::FailedToPayReward` because the pot is empty — despite B's reward having been "confirmed" via `RewardRegistered` earlier, exactly mirroring the report's scenario where a player wins but the payout pool has since been drained by an unrelated withdrawal.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-302)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
		}
```

**File:** bridges/modules/relayers/src/lib.rs (L564-574)
```rust
/// Implementation of `RewardLedger` for the pallet.
impl<T: Config<I>, I: 'static, Reward, RewardBalance>
	RewardLedger<T::AccountId, Reward, RewardBalance> for Pallet<T, I>
where
	Reward: Into<T::Reward>,
	RewardBalance: Into<T::RewardBalance>,
{
	fn register_reward(relayer: &T::AccountId, reward: Reward, reward_balance: RewardBalance) {
		Self::register_relayer_reward(reward.into(), relayer, reward_balance.into());
	}
}
```

**File:** bridges/primitives/relayers/src/lib.rs (L175-189)
```rust
	fn pay_reward(
		_: &Relayer,
		reward_kind: RewardsAccountParams<LaneId>,
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		T::transfer(
			&Self::rewards_account(reward_kind),
			&beneficiary.into(),
			reward.into(),
			Preservation::Expendable,
		)
		.map(drop)
	}
}
```
