Excellent match found: `pallet-staking-async`'s slash cancellation/apply-slash flow was exactly this bug class, and it was already fixed in this repo, mirroring the Astaria pattern (stale-parameter authentication allowing a canceled action to still be executed).

### Title
Permissionless `apply_slash` could manually apply governance-cancelled slashes (fixed by `CancelledSlashes` check) - (File: `substrate/frame/staking-async/src/pallet/mod.rs`)

### Summary
This is the exact local analog of the Seaport/Astaria bug class: a permissionless, public extrinsic authenticated a stale identifier (the `(era, validator, fraction, page)` slash key) against `UnappliedSlashes` storage without checking whether that specific slash had been canceled by governance, allowing an unprivileged caller to force-execute a "canceled auction"-equivalent (a canceled slash) and cause unbacked/duplicate settlement (slashing funds that governance had explicitly canceled).

### Finding Description
In `pallet_staking_async`, governance can cancel a deferred slash for a validator/era via `cancel_deferred_slash` [1](#0-0) , which records the cancellation in `CancelledSlashes` but leaves the underlying record in `UnappliedSlashes` untouched pending the era-rotation cleanup performed by `apply_unapplied_slashes` [2](#0-1) .

The permissionless fallback extrinsic `apply_slash` [3](#0-2)  takes `slash_era` and `slash_key` directly from the caller (analogous to the Seaport bug where `counterAtLiquidation` was caller-supplied) and looks up `UnappliedSlashes::<T>::take(&slash_era, &slash_key)`. Before the fix, this lookup did not re-validate against the current cancellation state — it trusted the stale, still-present `UnappliedSlashes` entry as authoritative, exactly like `CollateralToken.sol` trusting a stale Seaport counter to authenticate an already-canceled auction.

The current (fixed) code guards this with:
```rust
ensure!(
    !Self::check_slash_cancelled(slash_era, &slash_key.0, slash_key.1),
    Error::<T>::CancelledSlash
);
``` [4](#0-3) 

This is confirmed by the PRDoc explicitly describing the vulnerability and fix: `prdoc/stable2509/pr_9659.prdoc` — "Fix security vulnerability where the permissionless `apply_slash` extrinsic could be used to manually apply slashes that governance had cancelled via `cancel_deferred_slash`." [5](#0-4) 

### Impact Explanation
Before the guard existed, an unprivileged, unbonded caller could call `apply_slash` with the still-present stale `UnappliedSlashes` key for a validator whose slash governance had explicitly canceled, forcing an unbacked, unauthorized fund seizure (theft/loss of validator and nominator stake) that governance had intentionally reversed. This matches the "theft or unbacked mint/unlock" and "duplicate settlement" impact classes in scope, and directly parallels "canceled auctions can still be claimed."

### Likelihood Explanation
`apply_slash` is explicitly documented as permissionless ("anyone can call it") [6](#0-5) , so exploitation required no privileged access, malicious validator, or governance compromise — only knowledge of the `(slash_era, slash_key)` tuple, which is publicly queryable from `UnappliedSlashes` storage as documented in the function's own doc comment.

### Recommendation
The fix is already applied in this repository: `apply_slash` now calls `check_slash_cancelled` before consuming the `UnappliedSlashes` entry, and `apply_unapplied_slashes` performs the same check during normal era-rotation processing, ensuring slash execution always re-validates against the live `CancelledSlashes` state rather than trusting a stale, unguarded storage key. No further action needed beyond confirming this guard remains present in all `apply_slash`/`apply_unapplied_slashes` code paths.

### Proof of Concept
1. Validator `V` commits an offence in era `E`; a slash is queued in `UnappliedSlashes` at `slash_era = E + SlashDeferDuration` with key `(V, fraction, page)`.
2. Governance calls `cancel_deferred_slash(era, [(V, fraction)])`, recording the cancellation in `CancelledSlashes` (test `apply_slash_rejects_cancelled_slashes` demonstrates this setup) [7](#0-6) .
3. Prior to the fix, any signed account could call `apply_slash(slash_era, (V, fraction, page))` before era rotation processes/cleans up the entry, and the slash would be executed via `slashing::apply_slash` despite governance's cancellation — because the extrinsic did not check `CancelledSlashes`.
4. The fixed code path now returns `Error::<T>::CancelledSlash` for this exact call, as verified in the same test.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1596-1631)
```rust
	impl<T: Config> Pallet<T> {
		/// Apply previously-unapplied slashes on the beginning of a new era, after a delay.
		pub fn apply_unapplied_slashes(active_era: EraIndex) -> Weight {
			let mut slashes = UnappliedSlashes::<T>::iter_prefix(&active_era).take(1);
			if let Some((key, slash)) = slashes.next() {
				crate::log!(
					debug,
					"🦹 found slash {:?} scheduled to be executed in era {:?}",
					slash,
					active_era,
				);

				let nominators_slashed = slash.others.len() as u32;

				// Check if this slash has been cancelled
				if Self::check_slash_cancelled(active_era, &key.0, key.1) {
					crate::log!(
						debug,
						"🦹 slash for {:?} in era {:?} was cancelled, skipping",
						key.0,
						active_era,
					);
				} else {
					slashing::apply_slash::<T>(slash, Self::offence_era_of(active_era));
				}

				// Always remove the slash from UnappliedSlashes
				UnappliedSlashes::<T>::remove(&active_era, &key);

				// Check if there are more slashes for this era
				if UnappliedSlashes::<T>::iter_prefix(&active_era).next().is_none() {
					// No more slashes for this era, clear CancelledSlashes
					CancelledSlashes::<T>::remove(&active_era);
				}

				T::WeightInfo::apply_slash(nominators_slashed)
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2410-2443)
```rust
		#[pallet::call_index(17)]
		#[pallet::weight(T::WeightInfo::cancel_deferred_slash(validator_slashes.len() as u32))]
		pub fn cancel_deferred_slash(
			origin: OriginFor<T>,
			era: EraIndex,
			validator_slashes: Vec<(T::AccountId, Perbill)>,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;
			ensure!(!validator_slashes.is_empty(), Error::<T>::EmptyTargets);

			// Get current cancelled slashes for this era
			let mut cancelled_slashes = CancelledSlashes::<T>::get(&era);

			// Process each validator slash
			for (validator, slash_fraction) in validator_slashes {
				// Since this is gated by admin origin, we don't need to check if they are really
				// validators and trust governance to correctly set the parameters.

				// Remove any existing entry for this validator
				cancelled_slashes.retain(|(v, _)| v != &validator);

				// Add the validator with the specified slash fraction
				cancelled_slashes
					.try_push((validator.clone(), slash_fraction))
					.map_err(|_| Error::<T>::BoundNotMet)
					.defensive_proof("cancelled_slashes should have capacity for all validators")?;

				Self::deposit_event(Event::<T>::SlashCancelled { slash_era: era, validator });
			}

			// Update storage
			CancelledSlashes::<T>::insert(&era, cancelled_slashes);

			Ok(())
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L3055-3060)
```rust
		/// ## Behavior
		/// - The function is **permissionless**—anyone can call it.
		/// - The `slash_era` **must be the current era or a past era**.
		/// If it is in the future, the
		///   call fails with `EraNotStarted`.
		/// - The fee is waived if the slash is successfully applied.
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L3065-3087)
```rust
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

**File:** prdoc/stable2509/pr_9659.prdoc (L1-8)
```text
title: 'staking-async: prevent manual application of cancelled slashes'
doc:
- audience: Runtime Dev
  description: |-
      Fix security vulnerability where the permissionless `apply_slash` extrinsic could be used to manually apply slashes that governance had cancelled via `cancel_deferred_slash`.
crates:
- name: pallet-staking-async
  bump: major
```

**File:** substrate/frame/staking-async/src/tests/slashing.rs (L1270-1313)
```rust
#[test]
fn apply_slash_rejects_cancelled_slashes() {
	ExtBuilder::default().slash_defer_duration(2).build_and_execute(|| {
		// validator 11 has initial balance and is bonded
		assert_eq!(asset::stakeable_balance::<T>(&11), 1000);

		// Add a slash for validator 11 in era 1 (will be deferred to era 3)
		add_slash(11);
		Session::roll_next();
		let _ = staking_events_since_last_call();

		// Check current era
		assert_eq!(active_era(), 1);

		// Verify the slash is scheduled for era 3
		let slash_era = 3;
		let slash_key = (11, Perbill::from_percent(10), 0);
		assert!(UnappliedSlashes::<T>::contains_key(&slash_era, &slash_key));

		// Governance cancels this slash
		assert_ok!(Staking::cancel_deferred_slash(
			RuntimeOrigin::root(),
			slash_era,
			vec![(11, Perbill::from_percent(10))],
		));

		// Verify the cancellation event was emitted
		assert_eq!(
			staking_events_since_last_call(),
			vec![Event::SlashCancelled { slash_era, validator: 11 }]
		);

		// Verify the slash is cancelled
		let cancelled = CancelledSlashes::<T>::get(&slash_era);
		assert_eq!(cancelled, vec![(11, Perbill::from_percent(10))]);

		// Move to era 3 when the slash would be applied
		Session::roll_until_active_era(3);

		// Try to manually apply the cancelled slash - this should fail
		assert_noop!(
			Staking::apply_slash(RuntimeOrigin::signed(1), slash_era, slash_key),
			Error::<T>::CancelledSlash
		);
```
