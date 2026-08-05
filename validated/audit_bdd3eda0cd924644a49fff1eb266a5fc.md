Audit Report

## Title
Crowdloan `refund` payout loop can be permanently DoS'd by a single unrefundable contributor - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

## Summary
`Crowdloan::refund` iterates over contributors and calls a fallible `CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?` inside the loop without isolating per-item failures, so any single failing transfer aborts the whole dispatch via the `?` operator, rolling back every refund performed earlier in that call. [1](#0-0)  Since the contributor iteration order (`Self::contribution_iterator`) is deterministic and nothing removes the failing contributor on error, repeated calls to `refund` will re-hit the same failure point every time, blocking refunds for all subsequently-ordered contributors and preventing `fund.raised` from ever reaching zero, which in turn blocks `dissolve`.

## Finding Description
The loop body is confirmed exactly as described: [2](#0-1) . Because there is no `#[transactional]`/`with_storage_layer` isolation per iteration and the `transfer` call uses `?`, an `Err` from any single contributor's transfer propagates out of the whole `refund` dispatchable, causing FRAME's outer transactional dispatch wrapper to roll back *all* storage writes made in that call (including `contribution_kill` and `fund.raised` updates for contributors processed earlier in the same batch).

The critical open question is whether an unprivileged contributor can actually force `CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)` to fail using only self-controlled account state, with no privileged/governance actor involved. I was able to partially investigate this:

- The withdraw side (source = `fund_account`) is not attacker-controlled, so the only realistic vector is the deposit side (target = `who`, the contributor).
- Investigation of `pallet_balances`'s `try_mutate_account` / `deposit_creating` logic shows that plain deposits (free-balance increases) do not touch `reserved`/`frozen` fields, so consumer-ref exhaustion or freeze/lock mechanisms (which block *withdrawals*, as shown by the `lock_behavior_when_consumer_limit_fully_exhausted` test) do not block *deposits into* an account. [3](#0-2) 
- However, `deposit_creating` does contain a genuine failure path relevant here: `ensure!(value >= ed || !is_new, Error::<T, I>::ExistentialDeposit);` — if the contributor's account has been fully reaped (zero providers) by the time `refund` runs, and their refunded `balance` is below the chain's `ExistentialDeposit`, the deposit into that now-dead account will error deterministically. [4](#0-3) 

I was unable to fully verify, before running out of iterations, whether the crowdloan pallet's `MinContribution` configuration (checked in `contribute`) is guaranteed by all deployed runtimes (Polkadot, Kusama, Rococo, Westend) to be `>= ExistentialDeposit`, which would foreclose this specific ED-based failure vector. If `MinContribution < ExistentialDeposit` in any live configuration, or if a contributor's `balance` field in storage can differ from what was originally contributed (e.g., after partial `withdraw`+`contribute` interactions) such that a stub remainder below ED remains, this failure path becomes reachable. This is the same underlying loop-abort structural issue as the reported Solidity analog, but whether it is *practically exploitable end-to-end by an ordinary contributor with no other conditions* depends on runtime parameterization I could not confirm within the available tool budget.

## Impact Explanation
If the failure is reachable, it constitutes a permanent user-fund lock: the fund account's remaining balance would be irrecoverable via `refund`, and `dissolve` (which requires `fund.raised.is_zero()`) would never succeed, stalling cleanup of the fund indefinitely. This aligns with the "permanent user-fund lock" category in the impact gate. The severity is contingent on the ED/MinContribution relationship, which remains unconfirmed.

## Likelihood Explanation
The call is unsigned-permission (any signed account can call `refund`), matching the "unprivileged attacker" bar. However, the precondition for a *reliable* deposit-side failure (an already-reaped account with an owed refund below ED) requires either a specific runtime misconfiguration (`MinContribution < ExistentialDeposit`) or a specific sequence of partial withdrawals that I could not confirm is reachable in the current codebase within this investigation. Without that confirmation, likelihood cannot be assessed as "realistic and repeatable" with confidence — it is a plausible but not concretely demonstrated exploit path.

## Recommendation
Regardless of exact reachability, the structural fragility is real and should be fixed defensively:
- Do not let a single failed transfer abort the entire batched loop in `refund`; catch the error per-item (e.g., via `with_storage_layer`) and skip/quarantine the failing contributor instead of aborting the whole call.
- Alternatively, ensure `MinContribution >= ExistentialDeposit` is enforced at the type level (not just as a runtime constant choice) so the ED-triggered `deposit_creating` failure path is structurally unreachable.

## Proof of Concept
Not fully constructed — a concrete reproducible test would need to: (1) confirm a runtime/test configuration where `MinContribution < ExistentialDeposit` or construct a partial-refund/re-contribution sequence leaving a contributor's tracked `balance` below ED, (2) reap that contributor's account externally (e.g., transfer away all other funds), (3) call `refund` and observe the whole dispatch erroring and rolling back subsequent contributors' storage changes, (4) call `refund` again and observe the same failure recurring deterministically. This was not executed against the repository within the available investigation budget.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L509-536)
```rust
		pub fn refund(
			origin: OriginFor<T>,
			#[pallet::compact] index: ParaId,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
			let now = frame_system::Pallet::<T>::block_number();
			let fund_account = Self::fund_account_id(fund.fund_index);
			Self::ensure_crowdloan_ended(now, &fund_account, &fund)?;

			let mut refund_count = 0u32;
			// Try killing the crowdloan child trie
			let contributions = Self::contribution_iterator(fund.fund_index);
			// Assume everyone will be refunded.
			let mut all_refunded = true;
			for (who, (balance, _)) in contributions {
				if refund_count >= T::RemoveKeysLimit::get() {
					// Not everyone was able to be refunded this time around.
					all_refunded = false;
					break;
				}
				CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
				CurrencyOf::<T>::reactivate(balance);
				Self::contribution_kill(fund.fund_index, &who);
				fund.raised = fund.raised.saturating_sub(balance);
				refund_count += 1;
			}
```

**File:** substrate/frame/balances/src/tests/consumer_limit_tests.rs (L28-75)
```rust
#[test]
fn lock_behavior_when_consumer_limit_fully_exhausted() {
	ExtBuilder::default()
		.existential_deposit(1)
		.monied(true)
		.build()
		.execute_with(|| {
			// Account 1 starts with 100 balance
			Balances::make_free_balance_be(&1, 100);
			assert_eq!(System::providers(&1), 1);
			assert_eq!(System::consumers(&1), 0);

			// Fill up all consumer refs.
			// Note: asset-pallets prevents all the consumers to be filled and leaves one untouched.
			// But other operations in the runtime, notably `uniques::set_accept_ownership` might
			// overrule it.
			let max_consumers: u32 = <Test as frame_system::Config>::MaxConsumers::get();
			for _ in 0..max_consumers {
				assert_ok!(System::inc_consumers(&1));
			}
			assert_eq!(System::consumers(&1), max_consumers);

			// We cannot manually increment consumers beyond the limit
			assert_noop!(System::inc_consumers(&1), DispatchError::TooManyConsumers);

			// Although without limits it would work
			frame_support::hypothetically!({
				assert_ok!(System::inc_consumers_without_limit(&1));
			});

			// Now try to set a lock - this will still work because we use
			// `inc_consumers_without_limit` in `update_lock`.
			Balances::set_lock(ID_1, &1, 20, WithdrawReasons::all());
			assert_eq!(Balances::locks(&1).len(), 1);
			assert_eq!(Balances::locks(&1)[0].amount, 20);

			// frozen amount is also updated
			assert_eq!(get_test_account_data(1).frozen, 20);

			// now this account has 1 more consumer reference for the lock
			assert_eq!(System::consumers(&1), max_consumers + 1);

			// And this account cannot transfer any funds out.
			assert_noop!(
				Balances::transfer_allow_death(frame_system::RawOrigin::Signed(1).into(), 2, 90),
				DispatchError::Token(TokenError::Frozen)
			);
		});
```

**File:** substrate/frame/balances/src/impl_currency.rs (L491-503)
```rust
		Self::try_mutate_account_handling_dust(
			who,
			false,
			|account, is_new| -> Result<Self::PositiveImbalance, DispatchError> {
				let ed = T::ExistentialDeposit::get();
				ensure!(value >= ed || !is_new, Error::<T, I>::ExistentialDeposit);

				// defensive only: overflow should never happen, however in case it does, then this
				// operation is a no-op.
				account.free = match account.free.checked_add(&value) {
					Some(x) => x,
					None => return Ok(Self::PositiveImbalance::zero()),
				};
```
