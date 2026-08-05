Audit Report

## Title
Storage-deposit refund at contract termination pays the entire multi-contributor hold to the terminating transaction origin instead of each depositor - ([File: substrate/frame/revive/src/deposit_payment.rs])

## Summary
When a `pallet-revive` contract is terminated, `do_terminate` calls `T::Deposit::refund_all`, which by design bypasses the per-contributor `NativeDepositOf` accounting and releases the entire accumulated storage-deposit hold to a single destination — the transaction's dispatch origin — rather than to each account that actually paid for the storage. Any account whose calls caused storage growth in a shared/multi-tenant contract can have its deposit swept by whichever unrelated account happens to trigger termination.

## Finding Description
Storage writes are charged per-payer and tracked in `NativeDepositOf[contract][payer]`, so multiple unrelated callers can each fund distinct portions of a single contract's storage hold. At termination, `Stack::do_terminate` in `substrate/frame/revive/src/exec.rs` calls `T::Deposit::refund_all(&contract_account, exec_config.funds(origin.account_id()?))`, where `origin` is the top-level dispatch origin executing the extrinsic/call (i.e., whichever account triggered the termination path), not necessarily any of the depositors. [1](#0-0) 

`refund_all`'s trait documentation and both implementations (`()` and `PGasDeposit`) explicitly state that they "ignore the per-contributor caps that govern partial refunds" and instead release the *whole* hold on the contract to the single supplied destination, on the rationale that "at termination there is one recipient and the contract is gone." [2](#0-1) [3](#0-2) 

This is exercised directly by the repository's own test `refund_all_drains_multi_contributor_native_hold`, where ALICE and CHARLIE each independently pay for their own storage growth via `growStorage()`, then ALICE calls `terminate`, and the assertion explicitly confirms ALICE's balance increases by at least the *combined* native hold (her own deposit plus CHARLIE's), while CHARLIE's contribution is never returned to CHARLIE. [4](#0-3) 

The `terminate` precompile call is a public, permissionless call path (`ISystem.terminate`), reachable by any contract logic triggered by any caller, with no requirement that the caller/terminator be one of the depositors. [5](#0-4) 

## Impact Explanation
This is a beneficiary-correctness violation on contract-held value: funds deposited by one user (e.g. CHARLIE) to pay for their own storage usage are settled to a different, unrelated user (e.g. ALICE) purely because that user happened to be the dispatch origin when termination occurred. This matches the "conserve value and settle exactly once to the rightful beneficiary and amount" requirement for contract-held value — here the corrupted value is the storage-deposit refund destination/amount, diverted from the rightful depositor (CHARLIE) to an unrelated party (ALICE). Any contract pattern where multiple distinct accounts pay for their own storage slots (shared/multi-tenant contracts, escrows, etc.) is exposed to this loss upon termination.

## Likelihood Explanation
The behavior is deterministic, requires no privileged role, and is demonstrated by the repository's own test suite as expected, current behavior rather than a randomly-triggerable edge case. It requires only two ordinary unrelated accounts interacting with a contract that lets each pay for its own storage, followed by any account (including a non-depositor) triggering the public `terminate` path. However, it should be noted that the code comments frame this as an explicit, documented design tradeoff ("the native cap only makes sense for partial refunds on a live contract; at termination there is one recipient and the contract is gone") rather than an unnoticed bug — the repository authors were aware of and accepted this single-recipient-at-termination semantics, and shipped a test that asserts it as intended behavior rather than a regression to fix.

## Recommendation
If per-contributor refund correctness at termination is desired, `refund_all` should iterate `NativeDepositOf[contract][*]` and settle each contributor's proportional share back to that contributor individually, rather than sending the full hold to a single `dst`. Alternatively, restrict `terminate` such that only the sole depositor (or the original code-deposit payer) may trigger the refund-bearing termination path, or require the beneficiary be validated against depositor records before releasing funds.

## Proof of Concept
Reproduced verbatim by the existing repository test `refund_all_drains_multi_contributor_native_hold`: ALICE deploys `MultiContributorStorage`; ALICE calls `growStorage()`; CHARLIE (unrelated) calls `growStorage()`, each recorded under `NativeDepositOf[contract][ALICE]` and `NativeDepositOf[contract][CHARLIE]` respectively; ALICE then calls `terminate(beneficiary)`; the test asserts ALICE's balance increases by at least the combined native hold of both depositors, and the full hold is released from the contract account, with no return path to CHARLIE. [4](#0-3)

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1809-1812)
```rust
		let mut delete_contract = |trie_id: &TrieId, code_hash: &H256| {
			// deposit needs to be removed as it adds a consumer
			let refund =
				T::Deposit::refund_all(&contract_account, exec_config.funds(origin.account_id()?))?;
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L120-132)
```rust
	/// Refund every storage-deposit fund held on `from` to `dst`, ignoring the per-contributor
	/// caps that govern partial refunds. Used at contract termination.
	///
	/// Returns the total amount released, so the storage meter can finalise its deposit
	/// accounting.
	///
	/// # Parameters
	/// - `from`: contract whose hold is being released.
	/// - `dst`: destination of the refund. See [`Funds`].
	fn refund_all(
		from: &T::AccountId,
		dst: Funds<T::AccountId>,
	) -> Result<BalanceOf<T>, DispatchError>;
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L250-260)
```rust
	fn refund_all(
		from: &T::AccountId,
		dst: Funds<T::AccountId>,
	) -> Result<BalanceOf<T>, DispatchError> {
		let reason = HoldReason::StorageDepositReserve;
		let amount = T::Currency::balance_on_hold(&reason.into(), from);
		if !amount.is_zero() {
			<Self as Deposit<T>>::refund_on_hold(reason, from, dst, amount)?;
		}
		Ok(amount)
	}
```

**File:** substrate/frame/revive/src/tests/deposit_payment.rs (L460-527)
```rust
/// A contract whose storage was paid for by two different signers, both via the native
/// fallback path, can still be terminated. [`Deposit::refund_all`] bypasses the per-payer
/// [`NativeDepositOf`] cap (one recipient at termination, contract gone), so the full native
/// hold goes to the terminator and any PGAS hold is settled via `settle_pgas_refund`.
#[test_case(FixtureType::Solc)]
#[test_case(FixtureType::Resolc)]
fn refund_all_drains_multi_contributor_native_hold(fixture_type: FixtureType) {
	let (code, _) = compile_module_with_type("MultiContributorStorage", fixture_type).unwrap();
	ExtBuilder::default().build().execute_with(|| {
		Balances::set_balance(&ALICE, 100_000_000_000);
		Balances::set_balance(&CHARLIE, 100_000_000_000);

		let Contract { addr, account_id } =
			builder::bare_instantiate(Code::Upload(code)).build_and_unwrap_contract();

		assert_ok!(
			builder::bare_call(addr)
				.data(MultiContributorStorage::growStorageCall {}.abi_encode())
				.build()
				.result,
		);
		assert_ok!(
			BareCallBuilder::<Test>::bare_call(RuntimeOrigin::signed(CHARLIE), addr)
				.data(MultiContributorStorage::growStorageCall {}.abi_encode())
				.build()
				.result,
		);

		let alice_entry = NativeDepositOf::<Test>::get(&account_id, &ALICE);
		let charlie_entry = NativeDepositOf::<Test>::get(&account_id, &CHARLIE);
		assert!(alice_entry > 0);
		assert!(charlie_entry > 0);

		let hold: <Test as Config>::RuntimeHoldReason = HoldReason::StorageDepositReserve.into();
		let native_held = Balances::balance_on_hold(&hold, &account_id);
		let pgas_held = AssetsHolder::balance_on_hold(PGAS_ASSET_ID, &hold, &account_id);
		assert_eq!(pgas_held, 0, "every charge fell back to native");
		assert_eq!(native_held, alice_entry + charlie_entry);

		let alice_before = Balances::balance(&ALICE);
		assert_ok!(
			builder::bare_call(addr)
				.data(
					MultiContributorStorage::terminateCall { beneficiary: DJANGO_ADDR.0.into() }
						.abi_encode(),
				)
				.build()
				.result,
		);
		let alice_after = Balances::balance(&ALICE);

		assert!(get_contract_checked(&addr).is_none(), "contract should be gone");
		assert_eq!(
			Balances::balance_on_hold(&hold, &account_id),
			0,
			"the full multi-contributor native hold has been released",
		);
		// ALICE receives the full storage-deposit hold (her own + CHARLIE's). The actual delta
		// also picks up the code-upload deposit refund and any tx-level deposit accounting,
		// so it is at least `native_held`.
		assert!(
			alice_after.saturating_sub(alice_before) >= native_held,
			"expected ALICE balance delta >= {}, got {}",
			native_held,
			alice_after.saturating_sub(alice_before),
		);
	});
}
```

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L96-103)
```rust
			ISystemCalls::terminate(ISystem::terminateCall { beneficiary }) => {
				// no need to adjust gas because this always deletes code
				env.frame_meter_mut()
					.charge_weight_token(RuntimeCosts::Terminate { code_removed: true })?;
				let h160 = H160::from_slice(beneficiary.as_slice());
				env.terminate_caller(&h160).map_err(Error::try_to_revert::<T>)?;
				Ok(Vec::new())
			},
```
