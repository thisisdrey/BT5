Audit Report

## Title
Unauthenticated inflation of `InactiveIssuance` via direct transfers to `pallet-dap`'s public staging account - (File: `substrate/frame/dap/src/lib.rs`)

## Summary
`pallet_dap::Pallet::on_idle` drains whatever balance currently sits in the deterministically-derived `staging_account()` into `buffer_account()` and unconditionally calls `Unbalanced::deactivate(amount)` on the full drained amount, without tracking whether those funds actually originated from a genuine `OnUnbalanced` burn/slash credit rather than an ordinary `Balances` transfer. Since `staging_account()` is a well-known deterministic sub-account (`DAP_PALLET_ID` + `DAP_STAGING_ACCOUNT_ID`, also directly exposed by the `staging()` view function), any unprivileged account can send funds there via a normal transfer and have them treated as a burn, deflating `active_issuance()` (`TotalIssuance − InactiveIssuance`) without any actual token destruction.

## Finding Description
`on_nonzero_unbalanced` deposits genuine slash/burn credits into `staging_account()` via `T::Currency::resolve`. [1](#0-0) 

`on_idle` reads `reducible_balance(staging_account)` — the entire live balance of that account, regardless of provenance — moves it to `buffer_account()`, and calls `deactivate_buffer_funds`, which invokes `Unbalanced::deactivate(amount)`. [2](#0-1) [3](#0-2) 

There is no separate accounting/counter distinguishing "credited via `on_nonzero_unbalanced`" from "credited via an arbitrary `Balances::transfer_*`"; the pallet only inspects the account's current balance. The staging account address is also directly discoverable through the public `staging()` view function, in addition to being trivially re-derivable from the public constants `DAP_PALLET_ID` and `DAP_STAGING_ACCOUNT_ID`. [4](#0-3) [5](#0-4) 

The pallet's own test confirms that arbitrary balance in `staging_account()` is deactivated 1:1 on `on_idle`, with no origin or role gate: `slash_to_dap_accumulates_to_staging_then_deactivates_on_idle`. [6](#0-5) 

## Impact Explanation
`deactivate()` reduces `active_issuance()` without touching `TotalIssuance`, corrupting an accounting invariant that downstream reward/inflation logic (`mint_and_distribute`, which reads `T::Currency::total_issuance()` and feeds it to `IssuanceCurve::issue`) and any other `fungible::Inspect::active_issuance()` consumer relies on to reflect genuine burns only. [7](#0-6) 
An unprivileged attacker can deflate perceived active issuance system-wide at will simply by transferring their own funds to the known `staging_account()`, with no loss beyond the transferred amount ending up in `buffer_account()` (which is later redistributed per `BudgetAllocation`, potentially back to the attacker or to unrelated recipients). This is a runtime bug that compromises intended accounting behavior (`InactiveIssuance` corruption), matching the "runtime bugs that compromise intended behavior" impact class.

## Likelihood Explanation
High. The only prerequisites are: (1) knowledge of `staging_account()` — trivially obtainable via the public `staging()` view function or by deriving it from the public `DAP_PALLET_ID`/`DAP_STAGING_ACCOUNT_ID` constants — and (2) a standard signed `Balances::transfer_allow_death`/`transfer_keep_alive` extrinsic, which any account can submit without special timing, privilege, or front-running. `on_idle` executes automatically whenever block weight permits, so the exploit is repeatable at will.

## Recommendation
Do not rely on the raw live balance of `staging_account()` as the deactivation amount. Instead, track the specific amount of genuinely-burned funds credited via `on_nonzero_unbalanced` in dedicated pallet storage, and have `on_idle` drain/deactivate only that tracked amount (transferring/reconciling any surplus balance separately without deactivating it). Alternatively, make the staging account non-transferable from arbitrary signed extrinsics, or net out unexpected inflows against a running "expected from burns" counter before calling `deactivate`.

## Proof of Concept
1. Runtime wires `pallet_dap::Pallet<R>` (or `DapLegacyAdapter`) as `OnUnbalanced`/`Slash` for `pallet_balances`/`pallet_staking`, as is done for the runtimes referencing `pallet_dap` in this repo (e.g. `cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs`, `substrate/bin/node/runtime/src/lib.rs`).
2. Attacker calls the `dap` pallet's `staging()` view function (or derives `DAP_PALLET_ID.into_sub_account_truncating(DAP_STAGING_ACCOUNT_ID)` directly) to obtain `staging_account()`.
3. Attacker submits `Balances::transfer_keep_alive(staging_account, X)` from their own funded account.
4. On the next `on_idle`, `Pallet::<T>::on_idle` reads the now-inflated `reducible_balance(staging_account)`, moves `X` to `buffer_account()`, and calls `deactivate_buffer_funds(available)`, reducing `active_issuance()` by `X` even though no genuine slash/burn occurred and `TotalIssuance` is unchanged — reproducing the same mechanics shown in `slash_to_dap_accumulates_to_staging_then_deactivates_on_idle`, but with attacker-supplied non-burn funds as input. [6](#0-5)

### Citations

**File:** substrate/frame/dap/src/lib.rs (L214-248)
```rust
		fn on_idle(_block: BlockNumberFor<T>, remaining_weight: Weight) -> Weight {
			let mut meter = WeightMeter::with_limit(remaining_weight);

			// Need at least one read (staging account balance).
			if meter.try_consume(T::DbWeight::get().reads(1)).is_err() {
				return meter.consumed();
			}

			let staging_account = Self::staging_account();
			let available = T::Currency::reducible_balance(
				&staging_account,
				Preservation::Preserve,
				Fortitude::Polite,
			);

			if available.is_zero() {
				return meter.consumed();
			}

			// Need 1 read and 2 writes for the transfer, plus 1 read and 1 write for
			// deactivate (InactiveIssuance) and 1 read for TotalIssuance.
			if meter.try_consume(T::DbWeight::get().reads_writes(3, 3)).is_err() {
				return meter.consumed();
			}

			let buffer = Self::buffer_account();
			if T::Currency::transfer(&staging_account, &buffer, available, Preservation::Preserve)
				.is_err()
			{
				defensive!("DAP: staging account transfer to buffer failed");
				return meter.consumed();
			}

			Self::deactivate_buffer_funds(available);
			Self::deposit_event(Event::StagingDrained { amount: available });
```

**File:** substrate/frame/dap/src/lib.rs (L334-339)
```rust
		/// Account that holds burned/slashed funds before they are drained into
		/// the DAP buffer by `on_idle`. Exposed to clients so they don't have to
		/// re-derive the sub-account themselves.
		pub fn staging() -> T::AccountId {
			Self::staging_account()
		}
```

**File:** substrate/frame/dap/src/lib.rs (L359-362)
```rust
		/// Deactivate funds on buffer inflow.
		pub(crate) fn deactivate_buffer_funds(amount: BalanceOf<T>) {
			<T::Currency as Unbalanced<T::AccountId>>::deactivate(amount);
		}
```

**File:** substrate/frame/dap/src/lib.rs (L411-417)
```rust
		pub(crate) fn mint_and_distribute(elapsed: u64) -> BalanceOf<T> {
			let total_issuance = T::Currency::total_issuance();
			let issuance = T::IssuanceCurve::issue(total_issuance, elapsed);

			if issuance.is_zero() {
				return BalanceOf::<T>::zero();
			}
```

**File:** substrate/frame/dap/src/lib.rs (L512-524)
```rust
impl<T: Config> OnUnbalanced<CreditOf<T>> for Pallet<T> {
	fn on_nonzero_unbalanced(amount: CreditOf<T>) {
		let staging = Self::staging_account();
		let numeric_amount = amount.peek();

		// Funds land in the staging account; `on_idle` will drain them into the buffer and
		// deactivate them there.  Deactivation is intentionally deferred so that active issuance
		// does not flicker down-then-up within the same block.
		let _ = T::Currency::resolve(&staging, amount).inspect_err(|_| {
			defensive!(
				"🚨 Failed to deposit slash to DAP staging account - funds burned, it should never happen!"
			);
		});
```

**File:** substrate/primitives/dap/src/lib.rs (L27-31)
```rust
/// The [`PalletId`] used to represent the central DAP pallet.
pub const DAP_PALLET_ID: PalletId = PalletId(*b"dap/buff");

/// Sub-account identifier used to derive the DAP staging account.
pub const DAP_STAGING_ACCOUNT_ID: &[u8] = b"staging";
```

**File:** substrate/frame/dap/src/tests/on_unbalanced.rs (L86-144)
```rust
fn slash_to_dap_accumulates_to_staging_then_deactivates_on_idle() {
	build_and_execute(true, || {
		set_default_budget_allocation();

		let buffer = DapPallet::buffer_account();
		let staging = DapPallet::staging_account();
		let ed = <Balances as Inspect<_>>::minimum_balance();

		let alice = account_id(1);
		let bob = account_id(2);
		let charlie = account_id(3);

		// Given: buffer and staging each have ED; users have balances.
		assert_eq!(Balances::free_balance(&buffer), ed);
		assert_eq!(Balances::free_balance(&staging), ed);
		let initial_active = <Balances as Inspect<_>>::active_issuance();
		let initial_total = <Balances as Inspect<_>>::total_issuance();

		// When: multiple slashes occur via OnUnbalanced (simulating staking slashes).
		for (who, amount) in [(&alice, 30u64), (&bob, 20), (&charlie, 50)] {
			let credit = <Balances as Balanced<_>>::withdraw(
				who,
				amount,
				Precision::Exact,
				Preservation::Preserve,
				Fortitude::Force,
			)
			.unwrap();
			DapPallet::on_unbalanced(credit);
		}

		// Then: funds land in staging, not buffer.
		assert_eq!(Balances::free_balance(&staging), ed + 100);
		assert_eq!(Balances::free_balance(&buffer), ed);

		// And: users lost their slashed amounts.
		assert_eq!(Balances::free_balance(&alice), 100 - 30);
		assert_eq!(Balances::free_balance(&bob), 200 - 20);
		assert_eq!(Balances::free_balance(&charlie), 300 - 50);

		// And: active issuance is NOT yet decreased (deactivation is deferred to on_idle).
		assert_eq!(<Balances as Inspect<_>>::active_issuance(), initial_active);

		// And: total issuance unchanged (funds moved, not destroyed).
		assert_eq!(<Balances as Inspect<_>>::total_issuance(), initial_total);

		// When: on_idle drains staging into buffer and deactivates.
		DapPallet::on_idle(1, Weight::MAX);

		// Then: staging retains only ED; buffer gained all slashed funds.
		assert_eq!(Balances::free_balance(&staging), ed);
		assert_eq!(Balances::free_balance(&buffer), ed + 100);

		// And: active issuance decreased by 100 (funds deactivated in DAP buffer).
		assert_eq!(<Balances as Inspect<_>>::active_issuance(), initial_active - 100);

		// And: total issuance still unchanged.
		assert_eq!(<Balances as Inspect<_>>::total_issuance(), initial_total);
	});
```
