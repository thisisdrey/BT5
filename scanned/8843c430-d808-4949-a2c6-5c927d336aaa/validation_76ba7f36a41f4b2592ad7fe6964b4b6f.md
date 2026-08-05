## Analysis

The exact analog to the NFTXSimpleFeeDistributor "duplicate receiver" bug exists in `substrate/frame/dap/src/lib.rs` (the DAP — Dynamic Allocation Pool — pallet), combined with `substrate/primitives/staking/src/budget.rs`.

### Title
Duplicate `BudgetRecipient` keys cause repeated/unbacked minting because the only duplicate guard is compiled out in release builds - (File: `substrate/primitives/staking/src/budget.rs`, `substrate/frame/dap/src/lib.rs`)

### Summary
`pallet_dap::mint_and_distribute` mints newly issued tokens to every entry returned by `T::BudgetRecipients::recipients()` [1](#0-0) , using each entry's `BudgetKey` to look up its `Perbill` share from `BudgetAllocation`. The tuple-based `BudgetRecipientList` implementation is the only place that checks for duplicate keys, and it does so with `debug_assert!`, which is a no-op in release builds: [2](#0-1) . The second and only other guard, `Pallet::integrity_test`, uses a hard `assert!` but that hook only executes under test/try-runtime tooling, never during normal (release-mode) block production [3](#0-2) . If two `BudgetRecipient` implementations composed into the runtime's `BudgetRecipients` tuple ever share the same `BudgetKey` (e.g. via a copy-paste mistake during a runtime upgrade that wires in a new recipient), a live chain has no functioning safeguard against it.

### Finding Description
`mint_and_distribute` iterates over `recipients` (the list produced by `BudgetRecipientList::recipients()`) and, for each `(key, account)` pair, mints `perbill.mul_floor(issuance)` into `account`, where `perbill` is looked up from `BudgetAllocation` by `key` [1](#0-0) . Nothing in this loop deduplicates by key. If the same `BudgetKey` appears twice in `recipients` (pointing at the same or different `pot_account()`), the loop will mint that key's allocated `Perbill` share of `issuance` once per occurrence — i.e. the same percentage of newly-issued tokens gets minted multiple times per drip.

The only place a duplicate-key check exists is the blanket `BudgetRecipientList` impl for tuples, and it uses `debug_assert!`: [4](#0-3) . `debug_assert!` compiles to nothing when `debug-assertions` are disabled, which is the default for release/production builds of a Substrate-based chain's runtime WASM blob. The pallet's own `integrity_test` hook re-implements the same duplicate check with a real `assert!` [5](#0-4) , but `Hooks::integrity_test` is only invoked by test harnesses / `frame_support::traits::Hooks::integrity_test` calls in unit tests or CI tooling — it is never called as part of normal `on_initialize`/block-execution flow, so it provides zero protection to a running chain.

This precisely mirrors the reported bug class: a list of "receivers" (here, budget recipients) can contain duplicates because the enforcement code exists but is effectively disabled/ineffective in the environment that matters (there: no check at all guarding the owner-only `addReceiver`; here: a check that silently vanishes in release builds and a second check that never runs at runtime).

### Impact Explanation
`drip_issuance` runs every block/cadence via `on_initialize` [6](#0-5) , so a duplicate-key misconfiguration causes ongoing, compounding over-minting of the native asset every drip cycle for as long as the runtime remains misconfigured — this is unbacked mint inflating `TotalIssuance` beyond what `IssuanceCurve::issue` intended, breaking the "value conservation" invariant required for issuance/budget pallets and directly matching the "theft or unbacked mint" impact category.

### Likelihood Explanation
The trigger is not attacker-controlled in the traditional sense (no public extrinsic accepts arbitrary duplicate keys — `set_budget_allocation`'s `BudgetAllocationMap` is a `BoundedBTreeMap` which inherently rejects duplicate keys [7](#0-6) ). The exposure comes purely from how `type BudgetRecipients` is wired as a tuple in the runtime crate (a compile-time decision, potentially changed during runtime upgrades that add new pallets/recipients). The bug's danger is that the pallet's documented safety net (both the `debug_assert!` and `integrity_test`) gives false confidence to runtime maintainers that duplicates are caught, when in fact neither check runs against a production runtime binary — making an accidental duplicate silently pass and directly cause unbacked minting with no on-chain alarm.

### Recommendation
Replace the `debug_assert!` in `BudgetRecipientList`'s blanket tuple implementation with an actual `Result`-returning validation (or a hard runtime panic that isn't stripped in release), and additionally deduplicate by key at the start of `mint_and_distribute` (e.g. collapse `recipients` into a `BTreeMap<BudgetKey, AccountId>` before minting, or `ensure!` no duplicate keys exist and skip/halt minting if found) so a misconfiguration cannot cause double minting even if introduced at runtime-upgrade time. Also consider running the duplicate check unconditionally in `on_runtime_upgrade`/genesis-build so it fails fast rather than relying solely on a hook that never executes against a live release-mode chain.

### Proof of Concept
1. In a runtime, configure `type BudgetRecipients = (RecipientA, RecipientAAgain, ...)` in `pallet_dap::Config`, where `RecipientA` and `RecipientAAgain` both implement `sp_staking::budget::BudgetRecipient` and return the identical `budget_key()` (e.g. `b"validators"`) but different `pot_account()`s (accidental, e.g. copy-pasted during a runtime upgrade that adds a second incentive pot for the same category).
2. Build the runtime in release mode (`debug-assertions = false`, the standard for production WASM blobs) — the `debug_assert!` in `budget.rs` (lines 84-91) compiles away; `integrity_test` (which would have caught this) is never invoked outside of test harnesses.
3. Governance sets `BudgetAllocation` via `set_budget_allocation` giving `b"validators"` a nonzero `Perbill` share, e.g. `Perbill::from_percent(50)` — this call succeeds because `set_budget_allocation`'s own validation (lines 297-306) only checks that keys are *registered*, not that they're unique across recipients.
4. On each subsequent `on_initialize`, `mint_and_distribute` (lines 411-463) iterates `recipients` and mints `50%` of `issuance` to `RecipientA::pot_account()` AND another `50%` of `issuance` into `RecipientAAgain::pot_account()` — i.e., `100%` of `issuance` is minted for what was intended to be a single `50%` allocation, doubling that category's real issuance share every drip indefinitely, with `Event::IssuanceMinted.total_minted` reflecting the inflated (unbacked) total.

### Citations

**File:** substrate/frame/dap/src/lib.rs (L208-212)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		fn on_initialize(_n: BlockNumberFor<T>) -> Weight {
			Self::drip_issuance()
		}
```

**File:** substrate/frame/dap/src/lib.rs (L258-273)
```rust
		fn integrity_test() {
			assert!(
				T::MaxElapsedPerDrip::get() > T::IssuanceCadence::get(),
				"MaxElapsedPerDrip must be greater than IssuanceCadence, \
				 otherwise every drip would be clamped below the cadence threshold."
			);

			// Ensure BudgetRecipients have no duplicate keys.
			let mut keys: Vec<_> =
				T::BudgetRecipients::recipients().into_iter().map(|(k, _)| k).collect();
			keys.sort();
			assert!(
				keys.windows(2).all(|w| w[0] != w[1]),
				"Duplicate BudgetRecipient key detected"
			);
		}
```

**File:** substrate/frame/dap/src/lib.rs (L291-312)
```rust
		pub fn set_budget_allocation(
			origin: OriginFor<T>,
			new_allocations: BudgetAllocationMap,
		) -> DispatchResult {
			T::BudgetOrigin::ensure_origin(origin)?;

			// Validate all keys are registered recipients.
			let registered: Vec<_> =
				T::BudgetRecipients::recipients().into_iter().map(|(k, _)| k).collect();
			for key in new_allocations.keys() {
				ensure!(registered.contains(key), Error::<T>::UnknownBudgetKey);
			}

			// Validate sum == 100%. Use u64 to avoid overflow when summing deconstructed Perbills.
			let total_parts: u64 = new_allocations.values().map(|p| p.deconstruct() as u64).sum();
			ensure!(total_parts == Perbill::one().deconstruct() as u64, Error::<T>::BudgetNotExact);

			BudgetAllocation::<T>::put(new_allocations.clone());
			Self::deposit_event(Event::BudgetAllocationUpdated { allocations: new_allocations });

			Ok(())
		}
```

**File:** substrate/frame/dap/src/lib.rs (L428-446)
```rust
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

**File:** substrate/primitives/staking/src/budget.rs (L78-94)
```rust
#[impl_trait_for_tuples::impl_for_tuples(1, 10)]
#[tuple_types_custom_trait_bound(BudgetRecipient<AccountId>)]
impl<AccountId> BudgetRecipientList<AccountId> for Tuple {
	fn recipients() -> Vec<(BudgetKey, AccountId)> {
		let mut v = Vec::new();
		for_tuples!( #( v.push((Tuple::budget_key(), Tuple::pot_account())); )* );
		debug_assert!(
			{
				let mut keys: Vec<_> = v.iter().map(|(k, _)| k.clone()).collect();
				keys.sort();
				keys.windows(2).all(|w| w[0] != w[1])
			},
			"Duplicate BudgetRecipient key detected"
		);
		v
	}
}
```
