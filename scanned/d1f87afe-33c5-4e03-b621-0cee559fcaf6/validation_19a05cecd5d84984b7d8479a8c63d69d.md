## Analysis

The Curve/Notional report reduces to this invariant break: **a payout function silently returns/no-ops when a required resource isn't configured or available, but the surrounding logic has no way to distinguish "nothing to pay" from "payment failed", and record-keeping treats the claim as processed regardless.**

The closest verifiable local analog is in `pallet-staking-async`'s new dual-mode (mint vs. transfer-from-pot) reward payout path.

### Title
Staking-async marks era reward page as claimed before payout succeeds, permanently losing staker/nominator rewards on pot-transfer failure - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
`do_payout_stakers_by_page` marks a validator's era reward page as claimed via `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` **before** the actual currency transfer to the stash/nominators occurs. The subsequent transfer, performed by `payout_from_provider` → `make_payout_from_provider` (non-minting mode) or `payout_legacy_mint` → `make_payout_legacy` (legacy mode), does not propagate failures back to the dispatchable: any `Transfer`/mint error is only logged and the function returns `None`, and the extrinsic still returns `Ok(...)`.

### Finding Description
In `substrate/frame/staking-async/src/pallet/impls.rs`: [1](#0-0) 
the page is flagged claimed unconditionally, prior to any transfer being attempted, based only on validation of era/page bounds and reward-point existence.

The mode selection then picks the transfer path: [2](#0-1) 

In non-minting mode, `make_payout_from_provider` performs a real currency transfer out of the era-specific pot, but on failure it merely logs and returns `None` — no error is surfaced: [3](#0-2) 

The era pot itself is funded by `snapshot_era_rewards`, which has the same silent-failure pattern: if the transfer from the general DAP pot to the era pot fails, the allocation is simply set to zero and logged, with no mechanism to prevent later payout attempts against an underfunded/absent pot: [4](#0-3) 

Because `set_rewards_as_claimed` happens unconditionally before the transfer, and the transfer's failure is swallowed rather than propagated, a stash whose era-pot transfer fails (e.g., insufficient reducible balance in the pot due to preservation/existential-deposit interactions, a mis-provisioned `PotAccountProvider`, or partial pot funding from a prior snapshot failure) receives **zero funds** yet the page is recorded as claimed. Any retry of `payout_stakers`/`payout_stakers_by_page` for that era/page will hit the `AlreadyClaimed` guard: [5](#0-4) 
so the reward becomes permanently unclaimable — the same "short-circuit on an unmet precondition silently drops the claim" pattern as the Curve/Gauge report, except here the bookkeeping additionally locks out any future retry.

### Impact Explanation
This directly matches the "duplicate settlement or payout" / "permanent user-fund lock" impact category: validators and nominators can lose their entire era reward allocation permanently through no fault of their own, with no error surfaced to operators or users (only a log line), since `ClaimedRewards` storage is updated regardless of transfer outcome.

### Likelihood Explanation
Triggering requires the era reward pot to be underfunded or the transfer to fail at payout time — plausible under `Preservation::Expendable`/`Preservation::Preserve` edge cases around the pot's existential deposit, under `PotAccountProvider` misconfiguration, or when `snapshot_era_rewards` itself only partially succeeds (its own transfer failure is also silently zeroed, per `reward.rs` above). This does not require a malicious peer, validator, or governance actor — it is a state/configuration edge condition reachable by any legitimate era-boundary and payout-claim sequence.

### Recommendation
Do not call `Eras::<T>::set_rewards_as_claimed` until after the transfer(s) in `payout_from_provider`/`payout_legacy_mint` are confirmed successful, or roll back the "claimed" marker (and re-emit an event / return an error) if the payout transfer fails, so users retain the ability to retry rather than permanently losing the reward.

### Proof of Concept
1. Configure `pallet-staking-async` in non-minting mode (`DisableMinting = true`) with a `PotAccountProvider`/`Currency` combination where the era pot's reducible balance can legitimately fall short of the computed payout (e configure a small buffer / interact with existential deposit dust so `T::Currency::transfer` in `make_payout_from_provider` returns `Err`).
2. Advance to the era boundary; observe `snapshot_era_rewards` logs a transfer failure and allocates zero to the era pot (or an insufficient amount) — no on-chain failure is surfaced.
3. Call `payout_stakers`/`payout_stakers_by_page` for the affected validator/era/page: `set_rewards_as_claimed` executes at `impls.rs:386` before the transfer; `make_payout_from_provider` then fails to transfer, logs the error, returns `None`, and the extrinsic still returns `Ok`.
4. Re-call `payout_stakers_by_page` for the same era/page: it now returns `Error::<T>::AlreadyClaimed`, confirming the reward is permanently unclaimable despite never having been paid.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-386)
```rust
		if Eras::<T>::is_rewards_claimed(era, &stash, page) {
			return Err(Error::<T>::AlreadyClaimed
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)));
		}

		Eras::<T>::set_rewards_as_claimed(era, &stash, page);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L451-473)
```rust
		// Determine whether to use dap payout or legacy path.
		let use_dap_payout =
			DisableMintingGuard::<T>::get().is_some_and(|guard_era| era >= guard_era);

		let nominator_payout_count: u32 = if use_dap_payout {
			Self::payout_from_provider(
				era,
				&stash,
				validator_staker_payout_for_page,
				&exposure,
				overview_own,
				reward_split.nominator_payout,
			)
		} else {
			Self::payout_legacy_mint(
				era,
				&stash,
				validator_staker_payout_for_page,
				&exposure,
				overview_own,
				reward_split.nominator_payout,
			)
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

**File:** substrate/frame/staking-async/src/reward.rs (L109-125)
```rust
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
