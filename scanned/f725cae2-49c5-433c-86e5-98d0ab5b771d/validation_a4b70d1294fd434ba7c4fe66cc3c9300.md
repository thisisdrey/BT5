## Title
`unbond` in `pallet-nomination-pools` couples principal unbonding to reward-claim success, permanently blocking withdrawal if the reward transfer fails - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The external report describes a Solidity adapter where unstaking implicitly triggers a reward claim (`_getReward`), and if that claim reverts (e.g., because reward collection is paused), the whole unstake call reverts too — trapping the user's principal and leaving rewards stuck in the adapter. The exact same coupling pattern exists in `pallet_nomination_pools::Pallet::unbond`: before releasing any principal, the extrinsic performs an implicit, mandatory reward payout via `Self::do_reward_payout(...)?`, and if that inner transfer fails for any reason, the entire `unbond` call — including the unrelated principal-unbonding logic — is aborted.

### Finding Description
`unbond` is documented as implicitly collecting the member's rewards "one last time" before starting the unbonding process: [1](#0-0) 

The call flow performs the reward claim first and propagates any error with `?`, which — combined with FRAME's transactional dispatch semantics — reverts the whole extrinsic (including all later `unbond` state changes) if the payout fails: [2](#0-1) 

The reward payout itself performs a `T::Currency::transfer` from the pool's dedicated reward account to the member with `Preservation::Preserve`, propagating any transfer error with `?`: [3](#0-2) 

This mirrors the Kodiak adapter bug exactly: an operation whose sole purpose is to release the user's principal (`_withdrawLocked` / `unbond`) is made conditional on an unrelated reward-claim sub-call succeeding (`_getReward` / `do_reward_payout`). If the reward-side transfer cannot complete — e.g. the reward account's actual balance is insufficient relative to computed `pending_rewards` (which can occur from rounding drift across successive `update_records`/`register_claimed_reward` calls, or after a commission claim reduces the reward account balance below what a subsequent member's reward calculation expects) — the member is unable to unbond their stake at all, not just unable to claim the reward.

There is no try/catch-equivalent (no `.ok()` swallow, no separate weight-bounded fallback) around the reward-payout step inside `unbond`, unlike the migration code path in the same pallet which explicitly tolerates and logs reward-transfer failures instead of reverting the whole operation: [4](#0-3) 

That migration path demonstrates the pallet authors are aware reward transfers can legitimately fail (dust, rounding) and designed a non-reverting fallback there — but the live, user-facing `unbond` extrinsic does not apply the same defensive pattern.

### Impact Explanation
If the implicit reward transfer inside `unbond` fails, the member's principal stake cannot be unbonded through the normal path. Since `withdraw_unbonded` requires a prior successful `unbond` to create unlocking chunks, this can result in a member's stake being effectively locked in the pool indefinitely (a permanent user-fund lock), which is an explicitly in-scope impact category (`permanent user-fund or bridge-state lock`). Unlike the Solidity adapter case (where funds merely sit in an intermediate adapter), here the principal itself — not just the reward — becomes unbondable.

### Likelihood Explanation
This does not require a malicious actor, admin, or governance action — it can arise from the reward-pool accounting itself (rounding across many `update_records`/`register_claimed_reward` calls, or interleaved commission claims via `claim_commission` depleting the reward account relative to what `do_reward_payout` computes for other members) triggering `Currency::transfer` to fail with `InsufficientBalance` under `Preservation::Preserve`. This is a plausible, unprivileged-triggered condition rather than a contrived edge case, matching the "legitimate feature that can be activated for various reasons" likelihood framing from the source report.

### Recommendation
Decouple the mandatory pre-unbond reward payout from the principal-unbonding path: make the reward claim best-effort (e.g., wrap `do_reward_payout` in a fallible-but-tolerant helper that logs/emits a specific event and continues on error rather than propagating `?`), matching the tolerant pattern already used in `substrate/frame/nomination-pools/src/migration.rs`. This ensures a member can always unbond their principal even if the piggy-backed reward payout cannot currently be settled.

### Proof of Concept
1. Set up a nomination pool with multiple members and let rewards accrue such that `RewardPool` accounting (`last_recorded_reward_counter`, `total_commission_pending`) and the reward account's actual liquid balance drift apart across several `claim_payout`/`claim_commission`/`bond_extra` calls (rounding in `current_reward_counter`/`pending_rewards` computations, per `substrate/frame/nomination-pools/src/lib.rs` `do_reward_payout`).
2. Have a member whose computed `pending_rewards` exceeds the reward account's actual transferable balance (post commission claims) call `unbond`.
3. `do_reward_payout`'s `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)` fails with `InsufficientBalance` (or the transfer would breach the frozen `FreezeReason::PoolMinBalance`), and `unbond` returns `Err` at line 2288, leaving the member's principal still fully bonded with no unlocking chunk created — despite the member having full rights to withdraw their principal.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2224-2226)
```rust
		/// Unbond up to `unbonding_points` of the `member_account`'s funds from the pool. It
		/// implicitly collects the rewards one last time, since not doing so would mean some
		/// rewards would be forfeited.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2273-2295)
```rust
			bonded_pool.ok_to_unbond_with(&who, &member_account, &member, unbonding_points)?;

			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3552-3563)
```rust
		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);

		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L1011-1023)
```rust
						.for_each(|(who, last_claim)| {
							let outcome = T::Currency::transfer(
								&reward_account,
								&who,
								last_claim,
								Preservation::Preserve,
							);

							if let Err(reason) = outcome {
								log!(warn, "last reward claim failed due to {:?}", reason,);
							} else {
								sum_paid_out = sum_paid_out.saturating_add(last_claim);
							}
```
