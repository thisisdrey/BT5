Audit Report

## Title
`pallet-dap` deactivates arbitrary user-transferred funds as if they were burn/slash proceeds, corrupting `InactiveIssuance` - ([File: substrate/frame/dap/src/lib.rs])

## Summary
`pallet-dap`'s staging account, returned by `Pallet::staging_account()` and exposed via the public view function `staging()`, is a plain, deterministically-derived `AccountId` with no special access control. [1](#0-0)  `on_idle` reads the account's entire `reducible_balance`, transfers it to the buffer account, and calls `deactivate_buffer_funds` on the whole amount without checking its provenance. [2](#0-1)  Since `deactivate` unconditionally mutates the global `InactiveIssuance` counter, any unprivileged account can permanently corrupt `InactiveIssuance`/`active_issuance()` by simply transferring tokens to the known staging address.

## Finding Description
The pallet documents `on_nonzero_unbalanced` as the intended path for funds entering the staging account — intercepting slashes, fees, and dust removal via `OnUnbalanced` and resolving the `Credit` there. [3](#0-2)  However, `staging_account()` is merely `DAP_PALLET_ID.into_sub_account_truncating(sp_dap::DAP_STAGING_ACCOUNT_ID)`, an ordinary `AccountId` that the balances pallet treats like any other account for transfer purposes, and it is deliberately made discoverable via the `staging()` view function. [4](#0-3)  Nothing in the pallet or in `pallet_balances`'s `Unbalanced`/`Mutate` implementations distinguishes "credit resolved from `on_nonzero_unbalanced`" from "balance received via a normal `transfer_keep_alive`/`transfer_allow_death` call" — both simply increase the account's `free` balance.

`on_idle` then computes `available = reducible_balance(staging_account, ...)` and transfers/deactivates that entire amount with no provenance check whatsoever: [5](#0-4)  `deactivate_buffer_funds` forwards straight to `fungible::Unbalanced::deactivate`, which is implemented in `pallet_balances` as an unconditional mutation of `InactiveIssuance` (clamped only by `TotalIssuance`), with no tracking of where the deactivated balance originated: [6](#0-5)  There is no dedicated storage counter tracking "amount credited via `OnUnbalanced`" separately from the account's raw balance, so any donated funds are indistinguishable from genuine slash/burn proceeds by the time `on_idle` runs.

## Impact Explanation
`InactiveIssuance` is a runtime-wide accounting primitive; `active_issuance() = TotalIssuance - InactiveIssuance` is relied upon by downstream logic (e.g., governance voting weight, staking calculations) as a measure of circulating/voting-eligible supply. An unprivileged account can permanently inflate `InactiveIssuance` and shrink `active_issuance()` by an arbitrary amount of its own tokens, corrupting this core accounting invariant with no privileged actor involved — a runtime bug that compromises intended behavior of the DAP pallet and the chain-wide issuance accounting it feeds into.

## Likelihood Explanation
The attack requires only a funded, unprivileged account: query `Dap::staging()` (or derive it deterministically off-chain) to get the target address, submit a standard `Balances::transfer_keep_alive` to it, then wait for a block with idle weight (which `on_idle` runs in routinely). No race conditions, special timing, or privileged access are needed, making this trivially and repeatably exploitable.

## Recommendation
Do not treat the full reducible balance of the staging account as ground truth for deactivation. Track exactly how much was credited via `on_nonzero_unbalanced` in a dedicated storage counter and drain/deactivate only that tracked amount in `on_idle`, rather than reading `reducible_balance` of a plain, publicly-transferable account. Alternatively, ensure the staging account cannot receive ordinary transfers (e.g., via a filter or by settling credits immediately instead of holding a spendable balance).

## Proof of Concept
1. Call the `Dap::staging()` view function to obtain the staging account address.
2. From any funded, unprivileged account, submit `Balances::transfer_keep_alive(staging_account, X)`.
3. Wait for a block where `on_idle` has spare weight; `Pallet::<T>::on_idle` reads `reducible_balance(staging_account) == X`, transfers `X` to the buffer account, and calls `deactivate_buffer_funds(X)`.
4. Observe `InactiveIssuance` increase by `X` and `active_issuance()` decrease by `X`, even though `X` originated from a plain user transfer rather than a genuine slash/burn/fee credit.

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

**File:** substrate/frame/dap/src/lib.rs (L334-357)
```rust
		/// Account that holds burned/slashed funds before they are drained into
		/// the DAP buffer by `on_idle`. Exposed to clients so they don't have to
		/// re-derive the sub-account themselves.
		pub fn staging() -> T::AccountId {
			Self::staging_account()
		}
	}

	impl<T: Config> Pallet<T> {
		/// The DAP buffer account.
		///
		/// Collects any burn source wired to it (staking slashes, unclaimed rewards, etc.)
		/// and its explicit budget allocation share.
		pub fn buffer_account() -> T::AccountId {
			T::PalletId::get().into_account_truncating()
		}

		/// The DAP staging account.
		///
		/// Incoming funds land here and are periodically drained and deactivated into the
		/// DAP buffer account by `on_idle`.
		pub fn staging_account() -> T::AccountId {
			sp_dap::DAP_PALLET_ID.into_sub_account_truncating(sp_dap::DAP_STAGING_ACCOUNT_ID)
		}
```

**File:** substrate/frame/dap/src/lib.rs (L512-529)
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
		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited {numeric_amount:?} to DAP staging account"
		);
	}
```

**File:** substrate/frame/balances/src/impl_fungible.rs (L179-184)
```rust
	fn deactivate(amount: Self::Balance) {
		InactiveIssuance::<T, I>::mutate(|b| {
			// InactiveIssuance cannot be greater than TotalIssuance.
			*b = b.saturating_add(amount).min(TotalIssuance::<T, I>::get());
		});
	}
```
