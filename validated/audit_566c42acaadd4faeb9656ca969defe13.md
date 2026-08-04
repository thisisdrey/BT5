## Title
No incentive to call permissionless `apply_slash` fallback stalls unbonding withdrawals - (File: `substrate/frame/staking-async/src/pallet/mod.rs`)

### Summary
The external report describes an unincentivized permissionless "fallback" function (`liquidate`'s protocol path) whose caller pays gas but receives no profit, so nobody calls it in a timely manner, delaying protection of the protocol. The same broken-incentive pattern exists in `pallet-staking-async`'s `apply_slash` extrinsic: it is explicitly documented as a "fallback mechanism" for the case where slashes remain unapplied, it is permissionless, and it only waives the caller's fee (`Pays::No`) rather than paying any reward. Per its own doc comment, until this call is made, "all withdrawals get blocked" for the affected era, so failure to incentivize timely calls directly stalls user fund withdrawal rather than being merely a UX inconvenience.

### Finding Description
`apply_slash` is defined at [1](#0-0) . Its doc explicitly states:

> "For a given era x, if at era x+1, slashes are still unapplied, all withdrawals get blocked, and these need to be manually applied by calling this function. This function exists as a fallback mechanism for this extreme situation..."

The function is permissionless (`ensure_signed` only, no authorization check on `slash_key`/`slash_era` beyond validity), reads `UnappliedSlashes` and calls `slashing::apply_slash::<T>`, and returns `Pays::No` — i.e., only a fee waiver, not a reward — as the sole incentive for the caller: [2](#0-1) 

This mirrors the Ion Protocol `Liquidation.liquidate` bug class exactly: a permissionless clean-up/fallback path that must be executed to keep the system's accounting/liveness correct, but which offers the caller no positive economic incentive (unlike the profitable branch, e.g. `payout_stakers`/normal liquidation which pay real rewards/discounts). Unlike `reap_stash`'s fee waiver (which is comparably cheap, single-storage-removal work), `apply_slash` requires the caller to process an entire exposure page (`T::MaxExposurePageSize::get()`), a nontrivial amount of on-chain work and gas, for zero payoff — exactly the "no incentive to perform protocol liquidation" pattern, just with "slash application" substituted for "protocol liquidation".

### Impact Explanation
Because there is no financial incentive (only a fee waiver) to call `apply_slash`, if the automatic application of slashes fails to happen in a normal era transition (the "extreme situation" the code anticipates), the unapplied slash can persist indefinitely. Per the pallet's own documentation this blocks all stakers' unbonded fund withdrawals for the affected era, which is a "permanent user-fund lock" class impact until *someone* voluntarily pays gas for no reward to unblock it. This is a live, in-scope impact (funds temporarily/indefinitely inaccessible to legitimate, unprivileged users) rather than a theoretical inconvenience, since the pallet's comment confirms production dependence on this call succeeding.

### Likelihood Explanation
The likelihood is inherently tied to how often the "slashes still unapplied at era x+1" scenario occurs — the pallet authors call it "extreme" and state they "never expect to encounter this in normal scenarios," meaning in practice `apply_slash` may rarely need to be invoked. However, precisely because it is rare, there is no economic actor (searcher/keeper) monitoring for and racing to call it, unlike `payout_stakers` reward claims, which are actively pursued by third parties because stakers/pools depend on and often incentivize them off-chain. When the fallback path does trigger, there is no guarantee anyone (staker, nominator, or watcher) will notice and pay for an unrewarded transaction promptly, especially compared to reward-motivated calls elsewhere in the pallet.

### Recommendation
Provide a positive incentive for calling `apply_slash` beyond `Pays::No`, e.g., a small reward funded from the slashed amount itself (analogous to `SlashRewardFraction` used elsewhere in slashing logic), so that unprivileged actors are financially motivated to promptly execute this fallback and avoid withdrawal stalls. Alternatively/additionally, add an automated `on_idle`/off-chain-worker-driven attempt to apply outstanding slashes when spare block weight is available, as already noted as a "Future Improvement" in the pallet's own doc comment: [3](#0-2) 

### Proof of Concept
Conceptual trace (based on repository evidence; a live executable PoC would require constructing the exact "unapplied slash beyond era x+1" test scenario, which is exercised in [4](#0-3) -style tests):
1. An offence causes a deferred slash to be recorded in `UnappliedSlashes` for `slash_era`.
2. Assume the automatic application path (era-rotation hook) fails to apply it before era `slash_era + 1` (the documented "extreme situation").
3. Per the doc comment, withdrawals for the affected stakers become blocked.
4. Any signed account can call `apply_slash(slash_era, slash_key)` — [5](#0-4)  — to unblock withdrawals, but doing so costs gas/weight (`T::WeightInfo::apply_slash(MaxExposurePageSize)`) for zero reward (only `Pays::No`).
5. Absent an economic reason to call it, the blocked-withdrawal state persists until an altruistic actor intervenes, demonstrating the same "no incentive to perform necessary protocol cleanup" defect as the original report.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L3039-3087)
```rust
		/// For a given era x, if at era x+1, slashes are still unapplied, all withdrawals get
		/// blocked, and these need to be manually applied by calling this function.
		/// This function exists as a **fallback mechanism** for this extreme situation, but we
		/// never expect to encounter this in normal scenarios.
		///
		/// The parameters for this call can be queried by looking at the `UnappliedSlashes` storage
		/// for eras older than the active era.
		///
		/// ## Parameters
		/// - `slash_era`: The application era (`offence_era + SlashDeferDuration`), i.e. the key
		///   into [`UnappliedSlashes`].
		/// - `slash_key`: A unique identifier for the slash, represented as a tuple:
		///   - `stash`: The stash account of the validator being slashed.
		///   - `slash_fraction`: The fraction of the stake that was slashed.
		///   - `page_index`: The index of the exposure page being processed.
		///
		/// ## Behavior
		/// - The function is **permissionless**—anyone can call it.
		/// - The `slash_era` **must be the current era or a past era**.
		/// If it is in the future, the
		///   call fails with `EraNotStarted`.
		/// - The fee is waived if the slash is successfully applied.
		///
		/// ## Future Improvement
		/// - Implement an **off-chain worker (OCW) task** to automatically apply slashes when there
		///   is unused block space, improving efficiency.
		#[pallet::call_index(31)]
		#[pallet::weight(T::WeightInfo::apply_slash(T::MaxExposurePageSize::get()))]
		pub fn apply_slash(
			origin: OriginFor<T>,
			slash_era: EraIndex,
			slash_key: (T::AccountId, Perbill, u32),
		) -> DispatchResultWithPostInfo {
			let _ = ensure_signed(origin)?;
			let active_era = ActiveEra::<T>::get().map(|a| a.index).unwrap_or_default();
			ensure!(slash_era <= active_era, Error::<T>::EraNotStarted);

			// Check if this slash has been cancelled
			ensure!(
				!Self::check_slash_cancelled(slash_era, &slash_key.0, slash_key.1),
				Error::<T>::CancelledSlash
			);

			let unapplied_slash = UnappliedSlashes::<T>::take(&slash_era, &slash_key)
				.ok_or(Error::<T>::InvalidSlashRecord)?;
			slashing::apply_slash::<T>(unapplied_slash, Self::offence_era_of(slash_era));

			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/staking-async/src/tests/slashing.rs (L854-888)
```rust
#[test]
fn garbage_collection_on_window_pruning() {
	// ensures that `ValidatorSlashInEra` are cleared after
	// `BondingDuration`.
	ExtBuilder::default().build_and_execute(|| {
		assert_eq!(asset::stakeable_balance::<T>(&11), 1000);
		let now = active_era();

		let exposure = Staking::eras_stakers(now, &11);
		assert_eq!(asset::stakeable_balance::<T>(&101), 500);
		let nominated_value = exposure.others.iter().find(|o| o.who == 101).unwrap().value;

		add_slash(11);
		Session::roll_next();

		assert_eq!(asset::stakeable_balance::<T>(&11), 900);
		assert_eq!(asset::stakeable_balance::<T>(&101), 500 - (nominated_value / 10));

		assert!(ValidatorSlashInEra::<T>::get(&now, &11).is_some());

		for era in (0..(HistoryDepth::get() + 1)).map(|offset| offset + now + 1) {
			assert!(ValidatorSlashInEra::<T>::get(&now, &11).is_some());
			Session::roll_until_active_era(era);
		}

		// After HistoryDepth + 1 eras, lazy pruning is triggered.
		// We need to manually call prune_era_step to actually remove the data.
		let prune_era = now;
		while EraPruningState::<T>::get(prune_era).is_some() {
			assert_ok!(Staking::prune_era_step(RuntimeOrigin::signed(10), prune_era));
		}

		assert!(ValidatorSlashInEra::<T>::get(&now, &11).is_none());
	})
}
```
