Audit Report

## Title
Unauthenticated deposits to the DAP staging account are trusted as legitimate burns, letting anyone deflate `InactiveIssuance`/active issuance at will - (File: `substrate/frame/dap/src/lib.rs`)

## Summary
`pallet-dap`'s `on_idle` hook reads whatever `reducible_balance` sits in `Pallet::staging_account()` [1](#0-0)  and unconditionally treats the full amount as a legitimate slash/burn credit, transferring it to the buffer account and calling `Self::deactivate_buffer_funds(available)` [2](#0-1) . Because `staging_account()` is a deterministic, ordinary `AccountId` derived via `into_sub_account_truncating` [3](#0-2)  with no receive-side filter, any unprivileged account can `Balances::transfer_keep_alive` funds to it and have them counted as deactivated issuance without any real slash, burn, or wired `OnUnbalanced` event occurring.

## Finding Description
Legitimate flows deposit slashes/burns into the staging account through `OnUnbalanced::on_nonzero_unbalanced`, which calls `T::Currency::resolve(&staging, amount)` [4](#0-3) . This is functionally indistinguishable, at the balance level, from an ordinary `pallet_balances::transfer` into the same account, since `resolve` and a normal transfer both simply increase the free balance of `staging_account`.

`on_idle` then reads `reducible_balance` of `staging_account` with `Preservation::Preserve, Fortitude::Polite` [5](#0-4)  — this is a raw balance query with no way to distinguish attacker-sent funds from real slash/burn credits. It transfers the entire `available` amount to the buffer account and calls `deactivate_buffer_funds(available)`, which invokes `Unbalanced::deactivate(amount)` [6](#0-5) . `deactivate` lowers `active_issuance` (`total_issuance - InactiveIssuance`) while `total_issuance` remains unchanged, per the pallet's own doc comment stating "Incoming funds are deactivated to exclude them from governance voting" [7](#0-6) .

There is no code anywhere in this pallet that binds the balance increase in `staging_account` to an actual `OnUnbalanced` callback invocation — no counter, no event tag, no provenance check. The `staging()` view function even publicly exposes the deterministic address so it requires no guessing [8](#0-7) . This matches the required invariant failure: state advances (deactivation, a governance-critical accounting figure) based purely on an unauthenticated balance delta rather than validated provenance of the triggering event.

## Impact Explanation
`Balances::InactiveIssuance`/active issuance is a governance-critical figure — `pallet_referenda`'s servicing extrinsics (`nudge_referendum_continue_not_confirming`/`_continue_confirming`) read `Balances InactiveIssuance` per their weight/storage annotations, meaning active issuance feeds directly into OpenGov support/approval threshold arithmetic. Any unprivileged holder can transfer an arbitrary self-chosen amount to the publicly-derivable staging account, causing `on_idle` to silently and repeatedly deflate active issuance without any real slash/burn/dust event, degrading the correctness of the governance-threshold denominator. This is a runtime bug that compromises intended accounting behavior (state-integrity of `InactiveIssuance`/active issuance) achievable by any unprivileged actor.

## Likelihood Explanation
The attack requires only an ordinary signed account and a standard `pallet_balances::transfer`/`transfer_keep_alive` call to a publicly queryable, deterministic address (`Pallet::staging()`). No governance, validator/collator privilege, relayer, or malicious peer assumption is needed. `on_idle` processes the deposit automatically on the next idle block, making the attack mechanical and repeatable, bounded only by the attacker's own funds (which move into the buffer account rather than being returned to the attacker, imposing an actual cost per repetition but no other barrier).

## Recommendation
Do not treat the raw `reducible_balance` of the staging account as ground truth for deactivation. Track the amount to deactivate explicitly at the point of `OnUnbalanced::on_nonzero_unbalanced` (e.g., a dedicated `PendingDeactivation` storage value incremented only inside the trusted callback), and have `on_idle` drain/deactivate exactly that tracked amount rather than whatever balance happens to sit in the account. Alternatively, prevent ordinary transfers into the staging account entirely (e.g., use a non-transferable/system account type) so only the pallet's own credit path can fund it.

## Proof of Concept
1. Query `pallet_dap::Pallet::<Runtime>::staging()` to obtain the deterministic staging account address.
2. From any funded, unprivileged account, submit `Balances::transfer_keep_alive(staging_account, X)` for an arbitrary `X`.
3. Allow the next idle block to trigger `pallet_dap::Pallet::<T>::on_idle`.
4. Observe: the buffer account's balance increases by `X`, and `Balances::InactiveIssuance` increases by `X` (active issuance decreases by `X`), identical to the legitimate slash-credit path demonstrated in the existing test `slash_to_dap_accumulates_to_staging_then_deactivates_on_idle` [9](#0-8)  — even though the deposit was an ordinary user-initiated transfer with no slash, burn, or wired `OnUnbalanced` event.

### Citations

**File:** substrate/frame/dap/src/lib.rs (L29-32)
```rust
//! - **Burn Collection**: Implements `OnUnbalanced` to intercept any burn source wired to it
//!   (staking slashes, transaction fees, dust removal, EVM gas rounding, etc.) and redirect funds
//!   into the buffer account. Incoming funds are deactivated to exclude them from governance
//!   voting.
```

**File:** substrate/frame/dap/src/lib.rs (L222-231)
```rust
			let staging_account = Self::staging_account();
			let available = T::Currency::reducible_balance(
				&staging_account,
				Preservation::Preserve,
				Fortitude::Polite,
			);

			if available.is_zero() {
				return meter.consumed();
			}
```

**File:** substrate/frame/dap/src/lib.rs (L239-248)
```rust
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

**File:** substrate/frame/dap/src/lib.rs (L351-357)
```rust
		/// The DAP staging account.
		///
		/// Incoming funds land here and are periodically drained and deactivated into the
		/// DAP buffer account by `on_idle`.
		pub fn staging_account() -> T::AccountId {
			sp_dap::DAP_PALLET_ID.into_sub_account_truncating(sp_dap::DAP_STAGING_ACCOUNT_ID)
		}
```

**File:** substrate/frame/dap/src/lib.rs (L359-362)
```rust
		/// Deactivate funds on buffer inflow.
		pub(crate) fn deactivate_buffer_funds(amount: BalanceOf<T>) {
			<T::Currency as Unbalanced<T::AccountId>>::deactivate(amount);
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

**File:** substrate/frame/dap/src/tests/on_unbalanced.rs (L85-144)
```rust
#[test]
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
