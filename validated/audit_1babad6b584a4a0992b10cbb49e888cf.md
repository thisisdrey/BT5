### Title
`fungible`/`fungibles::Mutate::transfer` returns the requested nominal amount instead of the actual balance delta moved - (File: `substrate/frame/support/src/traits/tokens/fungible/regular.rs`, `substrate/frame/support/src/traits/tokens/fungibles/regular.rs`)

### Summary
The C4 report shows `depositGlp()` trusting the *nominal* `tokenAmount` requested from a transfer instead of the *actual* amount received, which breaks when the token charges a fee-on-transfer. The exact same broken invariant — "return/trust the requested amount rather than the amount actually moved" — exists in the default `Mutate::transfer` implementation shared by every fungible/fungibles asset in the runtime (native `Balances`, `pallet-assets`, and any custom asset backend), and this return value is consumed as ground truth by numerous downstream pallets for accounting, events, and ledger updates.

### Finding Description
`Mutate::transfer` in both `fungible::regular` and `fungibles::regular` is implemented as: [1](#0-0) 

```rust
fn transfer(
    source: &AccountId,
    dest: &AccountId,
    amount: Self::Balance,
    preservation: Preservation,
) -> Result<Self::Balance, DispatchError> {
    let _extra = Self::can_withdraw(source, amount).into_result(preservation != Expendable)?;
    Self::can_deposit(dest, amount, Extant).into_result()?;
    if source == dest {
        return Ok(amount);
    }

    Self::decrease_balance(source, amount, BestEffort, preservation, Polite)?;
    // This should never fail as we checked `can_deposit` earlier. But we do a best-effort
    // anyway.
    let _ = Self::increase_balance(dest, amount, BestEffort);
    Self::done_transfer(source, dest, amount);
    Ok(amount)
}
```

The same pattern exists for multi-asset transfers: [2](#0-1) 

Both `decrease_balance` and `increase_balance` are invoked with `Precision::BestEffort`. Per the trait documentation, `BestEffort` reduces/increases "by the most that is possible, up to `amount`", and the actually-moved amount is returned as the function's `Ok` value: [3](#0-2) 

However, in `transfer()`, both return values are **discarded** (`?` on `decrease_balance` without capturing the actual amount debited, and `let _ =` on `increase_balance`). The function then unconditionally returns `Ok(amount)` — the caller-*requested* nominal amount — regardless of what was actually moved. The dust/ED test suite confirms `decrease_balance` under `BestEffort` can legitimately debit *more or less* than the nominal `amount` (e.g. full-balance reaping when crossing the existential deposit): [4](#0-3) [5](#0-4) 

`increase_balance` under `BestEffort` similarly caps below the nominal amount when it would breach `minimum_balance` or overflow, silently rounding to less than requested: [6](#0-5) 

This is structurally identical to the reported bug class: the function reports success with the *intended* amount while the *actual* underlying balance movement can diverge, and there is no balance-before/after reconciliation.

### Impact Explanation
Numerous pallets treat the `Ok(amount)` returned from `Mutate::transfer` as the authoritative, exactly-settled value and use it directly to update ledgers/events without re-reading actual balances:

- Nomination pools migration accumulates `sum_paid_out` using the nominal `last_claim` regardless of the real transfer outcome, and emits `PaidOut { payout: last_claim }` unconditionally: [7](#0-6) 
- staking-async updates the stash ledger by the nominal `amount` after calling `T::Currency::transfer`, assuming the transferred amount exactly matches what was requested: [8](#0-7) 
- `TransferFungible::force_transfer_all_assets` and other generic asset-migration helpers rely on the same trait method to move "all" funds, trusting the reported result.

Because `transfer()` never surfaces the real delta, any divergence between requested and actual balance movement (dust reaping, ED-driven full reap, `BestEffort` capping on the deposit side) is silently absorbed and reported as fully successful with the nominal amount — corrupting downstream accounting values such as pool reward ledgers, staking ledger `active`/`total`, and emitted event amounts, which no longer conserve value 1:1 with actual on-chain balance changes.

### Likelihood Explanation
This is not a hypothetical: the conformance tests themselves demonstrate that `BestEffort` decrease/increase legitimately produce actual amounts different from the nominal `amount` whenever an account's resulting balance would fall below `minimum_balance` (existential deposit) — a routine, attacker-triggerable condition (e.g., draining a reward pot or pool account down to near-ED, or repeatedly claiming small "dust" payouts). No privileged actor, malicious peer, or governance action is required; any user causing a transfer to interact with the ED boundary reproduces the divergence.

### Recommendation
In `Mutate::transfer` (both `fungible::regular` and `fungibles::regular`), capture the actual value returned by `decrease_balance`/`increase_balance` and return the *minimum* of the two actual amounts (or otherwise reconcile them), rather than blindly returning the caller-supplied `amount`. Downstream callers (`nomination-pools`, `staking-async`, migration helpers) should use the returned actual value for ledger/event updates instead of the value they originally requested.

### Proof of Concept
1. Fund an account `A` with a fungible asset such that `balance(A) = minimum_balance + amount` (i.e., transferring `amount` would leave `A` with exactly `minimum_balance`, but any additional dust drop reaps it — this is exactly the scenario in `decrease_balance_expendable`/`unbalanced_trait_decrease_balance_at_most_works_3`: [5](#0-4) ).
2. Call `Mutate::transfer(&A, &B, amount, Preservation::Expendable)` where the `decrease_balance(BestEffort)` call ends up debiting more than `amount` due to full-reap dust handling (as shown in the conformance test returning `balance_before` instead of `account_0_initial_balance`).
3. Observe that `transfer()` still returns `Ok(amount)` — the nominal value — even though the actual amount debited from `A` (and possibly credited to `B`, capped separately by `increase_balance(BestEffort)`) differs.
4. Any pallet (e.g. `nomination-pools` reward migration) that logs/accumulates using this return value will diverge from the true on-chain balance state.

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L168-197)
```rust
	/// Reduce the balance of `who` by `amount`.
	///
	/// If `precision` is [`Exact`] and it cannot be reduced by that amount for
	/// some reason, return `Err` and don't reduce it at all. If `precision` is [`BestEffort`], then
	/// reduce the balance of `who` by the most that is possible, up to `amount`.
	///
	/// In either case, if `Ok` is returned then the inner is the amount by which is was reduced.
	/// Minimum balance will be respected and thus the returned amount may be up to
	/// [`Inspect::minimum_balance()`] - 1` greater than `amount` in the case that the reduction
	/// caused the account to be deleted.
	fn decrease_balance(
		who: &AccountId,
		mut amount: Self::Balance,
		precision: Precision,
		preservation: Preservation,
		force: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let old_balance = Self::balance(who);
		let reducible = Self::reducible_balance(who, preservation, force);
		match precision {
			BestEffort => amount = amount.min(reducible),
			Exact => ensure!(reducible >= amount, TokenError::FundsUnavailable),
		}

		let new_balance = old_balance.checked_sub(&amount).ok_or(TokenError::FundsUnavailable)?;
		if let Some(dust) = Self::write_balance(who, new_balance)? {
			Self::handle_dust(Dust(dust));
		}
		Ok(old_balance.saturating_sub(new_balance))
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L321-339)
```rust
	fn transfer(
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(source, amount).into_result(preservation != Expendable)?;
		Self::can_deposit(dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(dest, amount, BestEffort);
		Self::done_transfer(source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L366-386)
```rust
	fn transfer(
		asset: Self::AssetId,
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(asset.clone(), source, amount)
			.into_result(preservation != Expendable)?;
		Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
		Self::done_transfer(asset, source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/conformance_tests/regular/unbalanced.rs (L112-125)
```rust
	// And reap the account when Precision::BestEffort
	assert_eq!(
		T::decrease_balance(
			&account_0,
			account_0_initial_balance,
			Precision::BestEffort,
			Preservation::Expendable,
			Fortitude::Polite,
		),
		Ok(balance_before),
	);
	// Account reaped
	assert_eq!(T::balance(&account_0), 0.into());
}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/conformance_tests/regular/unbalanced.rs (L180-196)
```rust
	// Increasing the bal below the ED errors when precision is Exact
	if T::minimum_balance() > 0.into() {
		assert_eq!(
			T::increase_balance(&account_0, T::minimum_balance() - 1.into(), Precision::Exact),
			Err(TokenError::BelowMinimum.into()),
		);
	}
	assert_eq!(T::balance(&account_0), 0.into());

	// Increasing the bal below the ED leaves the balance at zero when precision is BestEffort
	if T::minimum_balance() > 0.into() {
		assert_eq!(
			T::increase_balance(&account_0, T::minimum_balance() - 1.into(), Precision::BestEffort),
			Ok(0.into()),
		);
	}
	assert_eq!(T::balance(&account_0), 0.into());
```

**File:** substrate/frame/balances/src/tests/fungible_tests.rs (L189-207)
```rust
#[test]
fn unbalanced_trait_decrease_balance_at_most_works_3() {
	ExtBuilder::default().build_and_execute_with(|| {
		// free: 40, reserved: 60
		assert_ok!(Balances::write_balance(&1337, 100));
		assert_ok!(Balances::hold(&TestId::Foo, &1337, 60));
		assert_eq!(Balances::free_balance(1337), 40);
		assert_eq!(Balances::total_balance_on_hold(&1337), 60);
		assert_eq!(Balances::decrease_balance(&1337, 0, BestEffort, Expendable, Polite), Ok(0));
		assert_eq!(Balances::free_balance(1337), 40);
		assert_eq!(Balances::total_balance_on_hold(&1337), 60);
		assert_eq!(Balances::decrease_balance(&1337, 10, BestEffort, Expendable, Polite), Ok(10));
		assert_eq!(Balances::free_balance(1337), 30);
		assert_eq!(Balances::decrease_balance(&1337, 200, BestEffort, Expendable, Polite), Ok(29));
		assert_eq!(<Balances as fungible::Inspect<_>>::balance(&1337), 1);
		assert_eq!(Balances::free_balance(1337), 1);
		assert_eq!(Balances::total_balance_on_hold(&1337), 60);
	});
}
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L1011-1029)
```rust
						.for_each(|(who, last_claim)| {
							let outcome = T::Currency::transfer(
								&reward_account,
								&who,
								last_claim,
								Preservation::Preserve,
							);

							if let Err(reason) = outcome {
								log!(warn, "last reward claim failed due to {:?}", reason,);
							} else {
								sum_paid_out = sum_paid_out.saturating_add(last_claim);
							}

							Pallet::<T>::deposit_event(Event::<T>::PaidOut {
								member: who.clone(),
								pool_id: id,
								payout: last_claim,
							});
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L602-627)
```rust
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

		// For Staked destination, update ledger.
		if matches!(dest, RewardDestination::Staked) {
			if let Ok(mut ledger) = Self::ledger(Stash(stash.clone())) {
				ledger.active += amount;
				ledger.total += amount;
				let _ = ledger
					.update()
					.defensive_proof("ledger fetched from storage, so it exists; qed.");
			}
		}
```
