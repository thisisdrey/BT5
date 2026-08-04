## Analysis

The Beanstalk bug's core invariant is: **a critical piece of state that a public/user-triggered code path depends on is never initialized by the deployment/migration process, so normal user activity silently breaks accounting and destroys value instead of producing the intended effect.**

The closest verified local analog in this repository is in `pallet_accumulate_and_forward`, a pallet used by several system chains (Westend relay chain, Collectives, Coretime) to gather transaction fees, dust removals, and revenue into an "accumulation account" for periodic forwarding to another chain.

### Title
Unfunded `accumulate-and-forward` accumulation account permanently burns fees/dust/revenue instead of forwarding them - (File: `substrate/frame/accumulate-and-forward/src/lib.rs`)

### Summary
`pallet_accumulate_and_forward` requires that its derived `accumulation_account()` be pre-funded with the existential deposit (ED) *outside of the pallet itself* — either via the balances genesis config or a manual transfer [1](#0-0) . The pallet has no `genesis_build`/`on_runtime_upgrade` of its own to guarantee this. If a runtime integrates or migrates to this pallet without separately funding the account, every fee payment, dust-removal, or revenue credit routed through it is **silently and permanently burned** rather than accumulated for forwarding, exactly mirroring Beanstalk's "storage that should have been set during migration but wasn't."

### Finding Description
The pallet's `OnUnbalanced` implementation deposits any incoming credit into the accumulation account via `T::Currency::resolve`: [2](#0-1) 

If the account does not exist and the incoming amount is below the existential deposit, `resolve` fails; the code only logs a `defensive!` warning and drops the `Credit`, which burns it (reduces total issuance) instead of forwarding it: [3](#0-2) 

The `LegacyAdapter` path has the identical footgun — it only emits a `defensive!` warning before unconditionally calling `resolve_creating`, which silently burns the imbalance if it's below ED and the account is unfunded: [4](#0-3) 

Crucially, `defensive!` only panics in debug/test builds; in a production runtime it is a no-op log line, so the burn happens silently on-chain with no error surfaced to users or operators. The pallet provides no `on_runtime_upgrade`/`genesis_build` hook to self-fund the account — this is left entirely to runtime integrators, as confirmed by every consuming runtime manually adding the account to the balances genesis config: [5](#0-4) [6](#0-5) 

This is the direct structural analog of the Beanstalk bug: a piece of state (`accumulation_account` funding) that downstream logic (`EnrootFacet`/chop-equivalent = fee/dust/revenue forwarding) depends on, but which the migration/deployment process for the pallet does not itself guarantee gets set. If any runtime's migration set omits pre-funding when the pallet is newly added (as opposed to genesis, where it's easy to remember), the omission is invisible until funds start disappearing.

### Impact Explanation
Any ordinary, unprivileged user transaction that pays a fee, causes a dust removal on transfer, or (on Coretime chains) generates revenue routed through `AccumulateForward` will have that value permanently destroyed (burned from total issuance) rather than forwarded to the intended destination, as demonstrated by the pallet's own tests: [7](#0-6) 

This is not self-healing per-deposit: each burned credit starts from a fresh non-existent/zero-balance account, so the loss recurs on every sub-ED deposit until some single deposit happens to reach ED. This constitutes unbacked value destruction / broken conservation of value with no path to recovery for the affected chain's revenue/fee stream — directly matching the "duplicate settlement… or permanent fund lock" impact class, here manifesting as permanent fund destruction.

### Likelihood Explanation
The trigger condition (an unfunded accumulation account) requires only an integration/migration oversight, not any admin or governance misbehavior at the point of exploitation — the exploitation itself is fully unprivileged: any user paying a transaction fee or having their account reaped triggers the burn. Given the pallet is designed for reuse across multiple system chains and explicitly documents this as an external setup responsibility rather than enforcing it in code, the probability of at least one deployment omitting the required funding step (especially when the pallet is added to an *existing* runtime via upgrade rather than at genesis) is non-trivial, and the pallet's own doc comments acknowledge the silent-burn risk without providing an automated guard.

### Recommendation
Add a mandatory `on_runtime_upgrade`/`genesis_build` style safeguard inside `pallet_accumulate_and_forward` (or a companion migration) that provisions the accumulation account with the existential deposit whenever the pallet's storage version is bumped or at genesis, removing the silent dependency on runtime integrators remembering a manual step. At minimum, escalate the `defensive!` log to a hard failure/consumption of the credit into a recoverable pot instead of an unconditional burn on unfunded-account failure.

### Proof of Concept
1. Deploy a runtime that includes `pallet_accumulate_and_forward` wired as `OnUnbalanced` for `pallet_transaction_payment` and/or `pallet_balances::DustRemoval`, but the migration/genesis config integrating the pallet omits funding `accumulation_account()` with the ED (as required by the crate docs but not enforced anywhere in code).
2. Any user submits a normal signed extrinsic and pays a transaction fee smaller than the chain's existential deposit (common on low-fee system parachains), or a transfer reaps the sender's account leaving sub-ED dust.
3. `Pallet::<T>::on_nonzero_unbalanced` is invoked with this small `Credit`; `T::Currency::resolve(&accumulation_account, amount)` fails because the account doesn't exist and `amount < ED`.
4. The `inspect_err` only logs via `defensive!` (no-op in production); the `Credit` is dropped, reducing `total_issuance` — the fee/dust value is permanently destroyed instead of being forwarded, and this recurs for every subsequent sub-ED deposit as confirmed by the existing unit test `on_unbalanced_panics_when_accumulation_account_not_funded_and_deposit_below_ed` (panic only occurs in debug builds; in release the burn proceeds silently).

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L32-38)
```rust
//! ## Setup
//!
//! The accumulation account must be pre-funded with at least the existential deposit.
//! For new chains, include the account in the balances genesis config.
//! For existing chains, fund it via a manual transfer.
//!
//! If the accumulation account is not pre-funded, deposits below ED will be silently burned.
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L289-308)
```rust
impl<T: Config> OnUnbalanced<CreditOf<T>> for Pallet<T> {
	fn on_nonzero_unbalanced(amount: CreditOf<T>) {
		let accumulation_account = Self::accumulation_account();
		let numeric_amount = amount.peek();

		// Resolve should never fail because:
		// - can_deposit on destination succeeds assuming accumulation account is pre-funded with ED
		// - amount is guaranteed non-zero by the trait method signature
		// The only failure would be overflow on destination or unfunded account.
		let _ = T::Currency::resolve(&accumulation_account, amount).inspect_err(|_| {
			frame_support::defensive!(
				"🚨 Failed to deposit to accumulation account - funds burned, it should never happen!"
			);
		});

		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited {numeric_amount:?} to accumulation account"
		);
	}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L329-354)
```rust
impl<T: Config, C> OnUnbalanced<LegacyNegativeImbalance<T::AccountId, C>> for LegacyAdapter<T, C>
where
	C: Currency<T::AccountId>,
{
	fn on_nonzero_unbalanced(amount: LegacyNegativeImbalance<T::AccountId, C>) {
		let accumulation_account = Pallet::<T>::accumulation_account();
		let numeric_amount = amount.peek();
		// NOTE: `resolve_creating` is "infallible" because it returns `()`, but it silently burns
		// the imbalance if it is less than ED and the destination is empty. We guard against this
		// by making misconfigured runtimes clearly visible. See crate-level docs for the
		// pre-funding requirement.
		if C::total_balance(&accumulation_account).saturating_add(numeric_amount) <
			C::minimum_balance()
		{
			frame_support::defensive!(
				"🚨 LegacyAdapter: deposit to accumulation account will be silently burned — \
				 ensure the accumulation account is pre-funded with at least ED!"
			);
		}
		C::resolve_creating(&accumulation_account, amount);
		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited (legacy) {numeric_amount:?} to accumulation account"
		);
	}
}
```

**File:** polkadot/runtime/westend/src/genesis_config_presets.rs (L341-352)
```rust
	const ENDOWMENT: u128 = 1_000_000 * WND;
	const STASH: u128 = 100 * WND;

	build_struct_json_patch!(RuntimeGenesisConfig {
		balances: BalancesConfig {
			balances: endowed_accounts
				.iter()
				.map(|k: &AccountId| (k.clone(), ENDOWMENT))
				.chain(initial_authorities.iter().map(|x| (x.0.clone(), STASH)))
				.chain(core::iter::once((accumulation_account(), ExistentialDeposit::get())))
				.collect::<Vec<_>>(),
		},
```

**File:** cumulus/parachains/runtimes/coretime/coretime-westend/tests/tests.rs (L320-352)
```rust
#[test]
fn coretime_revenue_goes_to_accumulation_account() {
	use frame_support::traits::{fungible::Balanced, tokens::imbalance::OnUnbalanced};

	let accumulation_account =
		pallet_accumulate_and_forward::Pallet::<Runtime>::accumulation_account();
	let ed = ExistentialDeposit::get();
	let revenue = 1_000_000_000u128;

	ExtBuilder::<Runtime>::default()
		.with_collators(collator_session_keys().collators())
		.with_session_keys(collator_session_keys().session_keys())
		.with_balances(vec![(accumulation_account.clone(), ed)])
		.with_para_id(1005.into())
		.build()
		.execute_with(|| {
			let accumulation_before =
				<Balances as Inspect<AccountId>>::balance(&accumulation_account);

			// When: simulate coretime revenue via OnUnbalanced with an issued credit.
			let credit = <Balances as Balanced<AccountId>>::issue(revenue);
			<AccumulateForward as OnUnbalanced<_>>::on_unbalanced(credit);

			// Then: accumulation account receives the revenue.
			let accumulation_after =
				<Balances as Inspect<AccountId>>::balance(&accumulation_account);
			assert_eq!(
				accumulation_after,
				accumulation_before + revenue,
				"accumulation account should receive coretime revenue"
			);
		});
}
```

**File:** substrate/frame/accumulate-and-forward/src/tests/on_unbalanced.rs (L113-134)
```rust
#[test]
#[should_panic(expected = "Failed to deposit to accumulation account")]
fn on_unbalanced_panics_when_accumulation_account_not_funded_and_deposit_below_ed() {
	new_test_ext(false).execute_with(|| {
		let accumulation_account = AccumulateForwardPallet::accumulation_account();
		let ed = <Balances as Inspect<_>>::minimum_balance();

		// Given: accumulation account is not funded
		assert_eq!(Balances::free_balance(accumulation_account), 0);

		// When: deposit < ED -> triggers defensive panic
		let credit = <Balances as Balanced<u64>>::withdraw(
			&1,
			ed - 1,
			Precision::Exact,
			Preservation::Preserve,
			Fortitude::Force,
		)
		.unwrap();
		AccumulateForwardPallet::on_unbalanced(credit);
	});
}
```
