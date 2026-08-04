### Title
Duplicate `BudgetRecipient` keys cause silent double-minting of inflation shares - (File: `substrate/frame/dap/src/lib.rs`, `substrate/primitives/staking/src/budget.rs`)

### Summary
`pallet_dap`'s inflation-distribution engine relies on `BudgetRecipientList::recipients()` returning a set of `(BudgetKey, AccountId)` pairs with **unique** keys. The only place uniqueness is checked is a `debug_assert!` inside the blanket tuple implementation, which is compiled out in release builds, and a duplicate copy of the same check in `Pallet::integrity_test()`, which is a compile-time/test-time sanity hook that is never executed while a live chain is producing blocks. `mint_and_distribute()` then mints `perbill.mul_floor(issuance)` **independently for every entry returned by `recipients()`**, keyed only by `BudgetKey`. If two distinct `BudgetRecipient` implementations (i.e., two distinct pallets/pot accounts wired into `T::BudgetRecipients`) end up sharing the same `BudgetKey`, both accounts receive the full share allocated to that key — the exact "duplicate item silently double-counted" bug class from the KUMA `changePayees()` report, but here it produces unbacked additional minting instead of an inconsistent share ledger.

### Finding Description
- `BudgetRecipientList::recipients()` for tuples pushes `(Tuple::budget_key(), Tuple::pot_account())` for every tuple element and only *checks* uniqueness via `debug_assert!`: [1](#0-0) 

`debug_assert!` is a no-op in release/production builds (`cfg(not(debug_assertions))`), so on any runtime compiled with `--release` (which every production Substrate/Polkadot SDK chain is), duplicate keys pass through silently.

- The pallet re-implements the same check only inside `integrity_test()`: [2](#0-1) 

`integrity_test()` is a FRAME `Hooks` method that is invoked by the `construct_runtime!`-generated integrity test harness during `cargo test`, not during on-chain execution. It provides no protection once the runtime is deployed; it can also be entirely absent from CI coverage if the generated integrity test isn't wired into the test suite that's actually run.

- The consumer of `recipients()` that actually moves value is `mint_and_distribute()`, which iterates every returned `(key, account)` pair and independently looks up the `Perbill` share for `key`, then mints that share to `account`: [3](#0-2) 

There is no `BTreeSet`/dedup check on `key` inside `mint_and_distribute`, `budget_recipients()` (view function), or `set_budget_allocation` — each of these three call sites independently calls `T::BudgetRecipients::recipients()` and trusts it to be duplicate-free: [4](#0-3) [5](#0-4) 

If the concrete `BudgetRecipients` tuple type configured for a runtime contains two elements that return the same `BudgetKey` (e.g., a copy-paste error when wiring a new pallet's `BudgetRecipient` impl, two pallets sharing a hard-coded key literal, or a runtime upgrade that adds a recipient whose key collides with an existing one), then for that key: `budget.get(key)` returns the *same* `Perbill` share, and `mul_floor(issuance)` mints that **same amount into each of the colliding accounts**. The total minted for that drip becomes `N × intended_share` instead of `1 × intended_share`, where `N` is the number of colliding recipients — an unbacked over-mint of tokens directly analogous to the KUMA bug where duplicate payees caused `_totalShares` and per-payee accounting to diverge.

### Impact Explanation
This breaks the core invariant that `mint_and_distribute()` mints exactly `IssuanceCurve::issue(total_issuance, elapsed)` split among distinct recipients according to `BudgetAllocation`. A key collision causes real economic over-issuance (new tokens minted into more than one account for the same budgeted share) with no compensating burn — this is unbacked mint / duplicate settlement of an inflation payout, which falls squarely under "theft or unbacked mint" and "runtime bugs that compromise intended behavior" in the accepted impact categories. Because `IssuanceCurve` inflation and `BudgetAllocation` percentages are runtime-wide state affecting every token holder (dilution), the blast radius is chain-wide, not scoped to a single account.

### Likelihood Explanation
The corrupted value (`BudgetKey`) is derived from associated-type implementations of `BudgetRecipient` supplied at compile time by whoever wires the `BudgetRecipients` tuple in the runtime. This is not attacker-controlled per block, but the guard against the collision is provably absent in production: `debug_assert!` is stripped in release builds, and `integrity_test()` is a test-time hook not exercised by the live chain. Any accidental key collision introduced during runtime configuration/upgrade (a routine, non-adversarial, non-privileged-abuse event — simply a maintenance mistake, not "admin abuse" or "governance abuse" since it requires no malicious intent, just an unguarded runtime wiring error) will silently double-mint on every issuance drip (every block or every `IssuanceCadence`), continuously compounding the over-issuance until someone notices the anomaly in total issuance figures.

### Recommendation
Enforce uniqueness of `BudgetKey`s at a point that actually executes in production, not merely in `debug_assert!` or `integrity_test()`:
- Deduplicate/validate `T::BudgetRecipients::recipients()` inside `mint_and_distribute()` itself (e.g., collect into a `BTreeMap<BudgetKey, AccountId>` and `defensive!`/skip-mint on collision, or make `on_initialize`/`drip_issuance` a no-op with an emitted error event) rather than trusting the tuple to have been checked at compile time.
- Replace the `debug_assert!` in `substrate/primitives/staking/src/budget.rs` with a `frame_support::ensure!`/`Result`-returning uniqueness check so release builds cannot silently accept colliding keys.

### Proof of Concept
1. Configure a runtime's `T::BudgetRecipients` tuple with two `BudgetRecipient` implementations that return the same `BudgetKey` (e.g., two pallets both returning `BudgetKey::truncate_from(b"reward".to_vec())`) but different `pot_account()`s.
2. Build the runtime in release mode (`debug_assertions` disabled) so the `debug_assert!` in `budget.rs` is compiled out.
3. Set `BudgetAllocation` via `set_budget_allocation` assigning, e.g., `50%` to the colliding key — this passes because `set_budget_allocation` only checks `registered.contains(key)` and that percentages sum to 100%, both of which are satisfied.
4. Each issuance drip in `mint_and_distribute()` iterates `recipients()`, finds the colliding key twice, and calls `T::Currency::mint_into` for **both** distinct accounts with the **same** `perbill.mul_floor(issuance)` amount, minting roughly double the intended share every drip, verifiable via `Event::IssuanceMinted { total_minted, .. }` exceeding the `IssuanceCurve::issue` output for that period.

### Citations

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

**File:** substrate/frame/dap/src/lib.rs (L297-309)
```rust
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
```

**File:** substrate/frame/dap/src/lib.rs (L321-332)
```rust
		pub fn budget_recipients() -> Vec<(BudgetKey, T::AccountId, Perbill)> {
			let allocation = BudgetAllocation::<T>::get();

			T::BudgetRecipients::recipients()
				.into_iter()
				.map(|(key, account)| {
					let share = allocation.get(&key).copied().unwrap_or(Perbill::zero());

					(key, account, share)
				})
				.collect()
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
