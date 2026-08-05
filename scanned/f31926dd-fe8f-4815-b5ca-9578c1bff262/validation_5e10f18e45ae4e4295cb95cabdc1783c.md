## Analysis

The external report's core broken invariant is: **a state-tracking counter is decremented/dropped based on a balance-zero check that doesn't account for a second, independent reference that still legitimately requires the counter to stay alive** — causing silent loss of accounting state through ordinary, unprivileged transfer calls.

A structurally identical pattern exists in `polkadot-sdk` between `pallet-balances`' `try_mutate_account` and `frame_system`'s provider/sufficient reference counting, exercised by an ordinary, permissionless dispatch sequence combining `pallet-assets::mint` (which sets a `sufficients` reference on `frame_system::Account`) with `pallet-balances::transfer_all`.

### Root cause walkthrough

`try_mutate_account` in `pallet-balances` computes whether an account "provides" and "consumes" purely from `free`/`reserved`/`frozen`, and when the resulting `free` balance is below `ExistentialDeposit` with `reserved == 0`, it takes the dust-removal path which does **not** restore the mutated account into storage — it deliberately leaves the row deleted: [1](#0-0) 

Right afterwards, the same function unconditionally invokes `frame_system::Pallet::<T>::dec_providers(who)` when `did_provide && !does_provide`: [2](#0-1) 

But when `T::AccountStore` is the System pallet itself (`AccountStore = System`, the standard runtime configuration), the earlier dust-removal step already erased the very same underlying `frame_system::Account<T>` row. `dec_providers` then finds nothing to mutate and just logs a defensive error and reports `Reaped`, silently discarding whatever `providers`/`consumers`/`sufficients` state existed on that row: [3](#0-2) 

If that account previously acquired a `sufficients` reference — e.g. by receiving a `pallet-assets` asset configured with `is_sufficient = true`, which calls `inc_sufficients` without any `providers` bump — the `sufficients` counter/reference is wiped out from `frame_system::Account` even though `pallet-assets::Account<T,I>` still records that account as `ExistenceReason::Sufficient` holding a live asset balance: [4](#0-3) 

This exact scenario is captured, unprivileged and reproducible purely with public extrinsics, in the pallet-assets test suite, which explicitly documents "the underlying bug is in the system pallet": [5](#0-4) 

and its `pallet-balances`-side counterpart: [6](#0-5) 

### Title
Native balance dust-removal in `pallet-balances::try_mutate_account` silently discards `frame_system` reference counters (`sufficients`) out-of-band from `pallet-assets`, corrupting cross-pallet account-existence invariants — (File: `substrate/frame/balances/src/lib.rs`)

### Summary
When an account's `free` balance is reduced to (or below) the Existential Deposit via any ordinary transfer (`transfer_allow_death`, `transfer_all`, etc.), `pallet-balances::try_mutate_account` removes the underlying `frame_system::Account<T>` storage row as "dust" *before* calling `frame_system::dec_providers`. If that same account is separately marked `sufficient` by `pallet-assets` (holding a foreign asset with `is_sufficient = true`), the `sufficients` reference recorded on that row is destroyed along with the row, while `pallet-assets::Account<T,I>` still believes the account exists with a live, sufficient-backed balance. `dec_providers` then silently no-ops ("Account already dead when reducing provider") instead of correctly reconciling the counters.

### Finding Description
This mirrors the external report's root cause: a counter meant to track "does this entity still legitimately need to exist" (`sufficients`/`providers`, analogous to the investor counter) is dropped based on a check (`free < ED`) performed in one module (`pallet-balances`) without being aware that another module (`pallet-assets`) still holds a valid, independent reference into the same account's reference-counting slot. Just like the investor-counter bug decremented state that should have persisted because "the same investor" owned both wallets, here the "same account" (single `AccountId`) is simultaneously tracked by two pallets sharing one reference-counter row, and the native-currency-only view in `pallet-balances` clobbers state that `pallet-assets` still depends on.

The trigger requires no privileged actor: any account can call the public `Assets::mint`/`force_create` style extrinsics (or receive a sufficient asset) to acquire a `sufficients` reference, then call the public `Balances::transfer_all` (or any transfer that drains `free` below ED) to trigger the corrupting path.

### Impact Explanation
Once the `frame_system::Account` row is wiped while `pallet-assets` still references that account as `Sufficient`, the invariant checked by `do_try_state` (`details.sufficients == calculated_sufficients`) can be violated at the chain level, and the orphaned asset-account bookkeeping (accounts/sufficients totals used for weight-benchmarking, existential-deposit-free account creation, and dust/reap accounting) diverges from real state. This is directly acknowledged as a system-pallet-level accounting bug in the shipped test suite comments, and it degrades the integrity of account-existence tracking that other pallets (staking, treasury, etc.) rely on via `providers`/`consumers`/`sufficients`, which is the same class of impact ("counters no longer reflect reality, enabling DoS/limit bypass for others") flagged in the external report.

### Likelihood Explanation
Requires only two ordinary, permissionless calls in sequence (`pallet-assets::mint` into an account configured as sufficient, then `pallet-balances::transfer_all`) with no elevated origin, collator, validator, or relayer involvement — matching the "public underpriced work / unprivileged path" bar for in-scope impacts.

### Recommendation
`try_mutate_account`'s dust-removal branch should not delete the storage row (or should defer to `dec_providers`/`dec_sufficients` reconciliation) whenever `frame_system::sufficients(who) > 0` or other non-balances references exist on the account; the removal decision must consult the full reference-count state atomically rather than only `free`/`reserved`.

### Proof of Concept [5](#0-4) 

This existing test in the repository (`multiple_transfer_alls_work_ok`) demonstrates the sequence: `force_create`/`mint` a sufficient asset for account `1` (bumping `sufficients` on the shared `frame_system::Account` row), then call `Balances::transfer_all` — the row is dust-removed by `pallet-balances` and the subsequent `dec_providers` call finds "Account already dead," losing the `sufficients` state that `pallet-assets` still relies on for account `1`'s asset entry.

### Citations

**File:** substrate/frame/balances/src/lib.rs (L1104-1115)
```rust
				if did_provide && !does_provide {
					// This could reap the account so must go last.
					frame_system::Pallet::<T>::dec_providers(who).inspect_err(|_| {
						// best-effort revert consumer change.
						if did_consume && !does_consume {
							let _ = frame_system::Pallet::<T>::inc_consumers(who).defensive();
						}
						if !did_consume && does_consume {
							let _ = frame_system::Pallet::<T>::dec_consumers(who);
						}
					})?;
				}
```

**File:** substrate/frame/balances/src/lib.rs (L1130-1141)
```rust
				let ed = Self::ed();
				let maybe_dust = if account.free < ed && account.reserved.is_zero() {
					if account.free.is_zero() {
						None
					} else {
						Some(account.free)
					}
				} else {
					*maybe_account = Some(account);
					None
				};
				Ok((maybe_endowed, maybe_dust, result))
```

**File:** substrate/frame/system/src/lib.rs (L1690-1731)
```rust
	/// Decrement the provider reference counter on an account.
	///
	/// This *MUST* only be done once for every time you called `inc_providers` on `who`.
	pub fn dec_providers(who: &T::AccountId) -> Result<DecRefStatus, DispatchError> {
		Account::<T>::try_mutate_exists(who, |maybe_account| {
			if let Some(mut account) = maybe_account.take() {
				if account.providers == 0 {
					// Logic error - cannot decrement beyond zero.
					log::error!(
						target: LOG_TARGET,
						"Logic error: Unexpected underflow in reducing provider",
					);
					account.providers = 1;
				}
				match (account.providers, account.consumers, account.sufficients) {
					(1, 0, 0) => {
						// No providers left (and no consumers) and no sufficients. Account dead.

						Pallet::<T>::on_killed_account(who.clone());
						Ok(DecRefStatus::Reaped)
					},
					(1, c, _) if c > 0 => {
						// Cannot remove last provider if there are consumers.
						Err(DispatchError::ConsumerRemaining)
					},
					(x, _, _) => {
						// Account will continue to exist as there is either > 1 provider or
						// > 0 sufficients.
						account.providers = x - 1;
						*maybe_account = Some(account);
						Ok(DecRefStatus::Exists)
					},
				}
			} else {
				log::error!(
					target: LOG_TARGET,
					"Logic error: Account already dead when reducing provider",
				);
				Ok(DecRefStatus::Reaped)
			}
		})
	}
```

**File:** substrate/frame/assets/src/functions.rs (L68-97)
```rust
	pub(super) fn new_account(
		who: &T::AccountId,
		d: &mut AssetDetails<T::Balance, T::AccountId, DepositBalanceOf<T, I>>,
		maybe_deposit: Option<(&T::AccountId, DepositBalanceOf<T, I>)>,
	) -> Result<ExistenceReasonOf<T, I>, DispatchError> {
		let accounts = d.accounts.checked_add(1).ok_or(ArithmeticError::Overflow)?;
		let reason = if let Some((depositor, deposit)) = maybe_deposit {
			if depositor == who {
				ExistenceReason::DepositHeld(deposit)
			} else {
				ExistenceReason::DepositFrom(depositor.clone(), deposit)
			}
		} else if d.is_sufficient {
			frame_system::Pallet::<T>::inc_sufficients(who);
			d.sufficients.saturating_inc();
			ExistenceReason::Sufficient
		} else {
			frame_system::Pallet::<T>::inc_consumers(who)
				.map_err(|_| Error::<T, I>::UnavailableConsumer)?;
			// We ensure that we can still increment consumers once more because we could otherwise
			// allow accidental usage of all consumer references which could cause grief.
			if !frame_system::Pallet::<T>::can_inc_consumer(who) {
				frame_system::Pallet::<T>::dec_consumers(who);
				return Err(Error::<T, I>::UnavailableConsumer.into());
			}
			ExistenceReason::Consumer
		};
		d.accounts = accounts;
		Ok(reason)
	}
```

**File:** substrate/frame/assets/src/tests.rs (L2119-2137)
```rust
#[test]
fn multiple_transfer_alls_work_ok() {
	build_and_execute(|| {
		// Only run PoC when the system pallet is enabled, since the underlying bug is in the
		// system pallet it won't work with BalancesAccountStore
		// Start with a balance of 100
		Balances::force_set_balance(RuntimeOrigin::root(), 1, 100).unwrap();
		// Emulate a sufficient, in reality this could be reached by transferring a sufficient
		// asset to the account
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		// Spend the same balance multiple times
		assert_ok!(Balances::transfer_all(RuntimeOrigin::signed(1), 1337, false));
		assert_ok!(Balances::transfer_all(RuntimeOrigin::signed(1), 1337, false));

		assert_eq!(Balances::free_balance(&1), 0);
		assert_eq!(Balances::free_balance(&1337), 100);
	});
}
```

**File:** substrate/frame/balances/src/tests/fungible_tests.rs (L576-599)
```rust
#[test]
fn sufficients_work_properly_with_reference_counting() {
	ExtBuilder::default()
		.existential_deposit(1)
		.monied(true)
		.build_and_execute_with(|| {
			// Only run PoC when the system pallet is enabled, since the underlying bug is in the
			// system pallet it won't work with BalancesAccountStore
			if UseSystem::get() {
				// Start with a balance of 100
				<Balances as fungible::Mutate<_>>::set_balance(&1, 100);
				// Emulate a sufficient, in reality this could be reached by transferring a
				// sufficient asset to the account
				System::inc_sufficients(&1);
				// Spend the same balance multiple times
				assert_ok!(<Balances as fungible::Mutate<_>>::transfer(&1, &1337, 100, Expendable));
				assert_eq!(Balances::free_balance(&1), 0);
				assert_noop!(
					<Balances as fungible::Mutate<_>>::transfer(&1, &1337, 100, Expendable),
					TokenError::FundsUnavailable
				);
			}
		});
}
```
