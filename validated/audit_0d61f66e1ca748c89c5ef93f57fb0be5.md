Audit Report

## Title
Integer-division rounding to zero in InstaPool revenue-share payout permanently forfeits a contributor's entitled share - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

## Summary
`do_claim_revenue` computes a contributor's share of a timeslice's InstaPool revenue via raw integer division (`total_payout.saturating_mul(contributed_parts.into()).checked_div(&pool_record.private_contributions.into())`) with no fixed-point (`Perbill`/`Perquintill`) scaling, unlike the analogous payout logic in `pallet-staking` and `pallet-nomination-pools`. When `contributed_parts` (bounded by the 80-bit `CoreMask`) is small relative to `pool_record.private_contributions`, the division floors to zero, so the contributor receives no payout for that timeslice even though their contribution is fully and irreversibly consumed. [1](#0-0) 

## Finding Description
`InstaPoolContribution::<T>::take(region)` removes the caller's contribution record up front, so there is no way to retry the claim once the loop advances past a timeslice. [2](#0-1)  For each timeslice `r`, `region.begin` is advanced and `contribution.length` decremented unconditionally, regardless of the computed payout `p`; `pool_record.private_contributions` is also reduced by `contributed_parts` whether or not `p == 0`. [3](#0-2)  When `private_contributions` reaches zero after this reduction, the history record is deleted (`InstaPoolHistory::<T>::remove(r)`) even if `remaining_payout` (i.e., `total_payout - p`) is still nonzero, permanently discarding that residual amount from the accounting rather than returning it to any claimant. [4](#0-3)  This contrasts with `pallet-staking`'s use of `Perbill::from_rational` for reward-share computation [5](#0-4)  and `pallet-nomination-pools`'s documented precision floor via `FixedU128`/`U256`-scaled math. [6](#0-5) 

## Impact Explanation
This is a genuine rounding/precision defect that can cause an InstaPool contributor's rightful share of a timeslice's revenue to be silently zeroed while their entitlement is consumed with no recourse, and can also leave residual `remaining_payout` orphaned in the pallet account when the history record is deleted. However, the magnitude of loss per occurrence is inherently bounded by `contributed_parts` (at most 80, the bit-width of `CoreMask`) relative to `private_contributions`, making this a dust-level precision-loss issue structurally similar to the rounding floors that are already accepted and documented elsewhere in the codebase (e.g., `pallet-nomination-pools`'s `current_reward_counter`/`smallest_claimable_reward` design notes), rather than an attacker-amplifiable theft, duplicate-settlement, or large-scale fund-lock. It does not allow an attacker to mint, duplicate, or redirect funds to an unintended beneficiary; it only causes proportional, bounded rounding loss inherent to integer-based pro-rata division, a pattern common throughout Substrate reward/payout pallets.

## Likelihood Explanation
The condition (`contributed_parts * total_payout < private_contributions`) can occur naturally without any attacker action, triggered by any ordinary contributor calling the public `claim_revenue` extrinsic under normal operating conditions (e.g., many small contributors sharing a modest timeslice payout). No privileged access or malicious behavior is required to reach the code path.

## Recommendation
Replace the raw `saturating_mul`/`checked_div` computation with a fixed-point ratio (e.g., `Perquintill::from_rational(contributed_parts, private_contributions).mul_floor(total_payout)`) or `U256`-based scaled arithmetic consistent with `nomination-pools::point_to_balance`, and avoid deleting `InstaPoolHistory` records that still carry nonzero `remaining_payout` — instead route residual dust to a defined sink (e.g., treasury) rather than silently dropping it.

## Proof of Concept
1. Configure a timeslice where 100 single-core (`mask.count_ones() == 1`) private contributions exist, so `private_contributions = 100`.
2. Set `total_payout` for that timeslice to `50` (any value where `50 * 1 / 100 == 0` under integer division).
3. Each of the 100 contributors calls `claim_revenue` for their region covering this timeslice.
4. Observe `p == 0` for every contributor via `saturating_mul(1).checked_div(100) == 0`; no transfer occurs for any contributor for that timeslice, yet `pool_record.private_contributions` is decremented each time, and eventually `InstaPoolHistory::<T>::remove(r)` executes, discarding any nonzero leftover `remaining_payout` without it reaching any contributor. [7](#0-6)

### Citations

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L424-452)
```rust
		let mut contribution =
			InstaPoolContribution::<T>::take(region).ok_or(Error::<T>::UnknownContribution)?;
		let contributed_parts = region.mask.count_ones();

		Self::deposit_event(Event::RevenueClaimBegun { region, max_timeslices });

		let mut payout = BalanceOf::<T>::zero();
		let last = region.begin + contribution.length.min(max_timeslices);
		for r in region.begin..last {
			region.begin = r + 1;
			contribution.length.saturating_dec();

			let Some(mut pool_record) = InstaPoolHistory::<T>::get(r) else { continue };
			let Some(total_payout) = pool_record.maybe_payout else { break };
			let p = total_payout
				.saturating_mul(contributed_parts.into())
				.checked_div(&pool_record.private_contributions.into())
				.unwrap_or_default();

			payout.saturating_accrue(p);
			pool_record.private_contributions.saturating_reduce(contributed_parts);

			let remaining_payout = total_payout.saturating_sub(p);
			if !remaining_payout.is_zero() && pool_record.private_contributions > 0 {
				pool_record.maybe_payout = Some(remaining_payout);
				InstaPoolHistory::<T>::insert(r, &pool_record);
			} else {
				InstaPoolHistory::<T>::remove(r);
			}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L331-337)
```rust
		// This is the fraction of the total reward that the validator and the
		// nominators will get.
		let validator_total_reward_part =
			Perbill::from_rational(validator_reward_points, total_reward_points);

		// This is how much validator + nominators are entitled to.
		let validator_total_payout = validator_total_reward_part * era_payout;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1491-1509)
```rust
		// * accuracy notes regarding `checked_from_rational` collapsing to zero, meaning that no
		//   reward can be claimed:
		//
		// largest `bonded_points`, such that the reward counter is non-zero, with `FixedU128` will
		// be when the payout is being computed. This essentially means `payout/bonded_points` needs
		// to be more than 1/1^18. Thus, assuming that `bonded_points` will always be less than `10
		// * dot_total_issuance`, if the reward_counter is the smallest possible value, the value of
		//   the
		// reward being calculated is:
		//
		// x / 10^20 = 1/ 10^18
		//
		// x = 100
		//
		// which is basically 10^-8 DOTs. See `smallest_claimable_reward` for an example of this.
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;
```
