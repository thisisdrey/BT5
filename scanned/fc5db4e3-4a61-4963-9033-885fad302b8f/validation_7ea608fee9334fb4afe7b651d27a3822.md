Based on repository evidence, I found a strong local analog to the reported bug class ("privileged function that sweeps commingled funds — including value that belongs to third parties — to a single destination, breaking value-conservation for the rightful owners").

### Title
Contract termination drains *all* multi-contributor storage-deposit holds to a single beneficiary instead of refunding each depositor - ([File: substrate/frame/revive/src/deposit_payment.rs])

### Summary
`pallet-revive`'s storage-deposit accounting allows multiple, unrelated signed accounts to each pay (and hold) a native-currency storage deposit against the *same* contract, tracked per-payer in `NativeDepositOf<contract, payer>`. When the contract is terminated (via `SELFDESTRUCT`/`terminate_v1`/`ISystem.terminate`, all of which are ordinary permissionless contract calls), `Deposit::refund_all` explicitly bypasses this per-payer bookkeeping and transfers the *entire* held balance to a single destination account tied to the terminating call's origin — not to each original depositor.

### Finding Description
`do_terminate` calls `T::Deposit::refund_all(&contract_account, exec_config.funds(origin.account_id()?))?` [1](#0-0) . Both the native-only and PGAS-backed `refund_all` implementations pull the *full* `HoldReason::StorageDepositReserve` balance held on the contract and send it to a single `dst`, explicitly documented as bypassing the per-contributor cap: [2](#0-1) [3](#0-2) 

The repository's own test explicitly demonstrates the fund-conservation break: two different signers (`ALICE` and `CHARLIE`) independently pay storage deposits into the same contract via `growStorageCall`; when the contract is later terminated, ALICE (the terminator) receives *both* her own deposit *and* CHARLIE's, while CHARLIE never gets his deposit back: [4](#0-3) 

The per-payer cap (`NativeDepositOf`) exists specifically to prevent one contributor from claiming another's refund during the contract's *live* lifetime, but that guard is silently dropped exactly at the one point where the value actually leaves the system (termination) — the point where correctness matters most, since it is irreversible and there is no longer a contract account to make individuals whole afterward.

### Impact Explanation
Any contract whose storage is funded by more than one account (a common, legitimate pattern for shared/registry-style contracts where each user's storage entry is deposited by that user) allows whoever triggers termination — which is simply a normal, unprivileged contract call, not a chain-governance or admin action — to permanently redirect every other contributor's storage-deposit refund to themselves. This is a direct violation of the "conserve value and settle exactly once to the rightful beneficiary" invariant: the deposit refund settles to the wrong account and amount, and once the contract account is destroyed the loss is unrecoverable for the other depositors.

### Likelihood Explanation
Reachability requires no malicious validator, collator, relayer, or chain governance/admin action — only a contract whose owner (or anyone permitted by the contract's own logic to call terminate) triggers termination while other unrelated accounts have outstanding `NativeDepositOf` entries. Multi-contributor storage-deposit funding is an ordinary, expected usage pattern (the repo's own fixture `MultiContributorStorage` and test exist specifically to exercise it), so the precondition is easily and routinely reachable by an unprivileged actor.

### Recommendation
At termination, either (a) iterate `NativeDepositOf<contract, _>` and refund each contributor their own recorded share before sending only the true residual/excess to the beneficiary, or (b) explicitly document and gate this as an accepted, contract-author-level risk and require contracts to fully unwind third-party contributions before allowing termination, analogous to only ever sweeping *excess* value rather than commingled third-party balances.

### Proof of Concept
1. Deploy `MultiContributorStorage` (or any contract permitting multiple accounts to grow its storage), funded by `ALICE`.
2. `CHARLIE` calls a storage-growing method, causing a `NativeDepositOf(contract, CHARLIE)` hold to accrue on the contract.
3. `ALICE` (or whoever is authorized to call the contract's terminate method) calls `terminate(beneficiary=ALICE)`.
4. Observe, as in `refund_all_drains_multi_contributor_native_hold` [5](#0-4) , that ALICE's balance increases by at least `alice_entry + charlie_entry`, while CHARLIE receives nothing back for his contribution.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1809-1834)
```rust
		let mut delete_contract = |trie_id: &TrieId, code_hash: &H256| {
			// deposit needs to be removed as it adds a consumer
			let refund =
				T::Deposit::refund_all(&contract_account, exec_config.funds(origin.account_id()?))?;

			// we added this consumer manually when instantiating
			System::<T>::dec_consumers(&contract_account);

			// ED was minted when the account was brought into existence; burn it now.
			T::Deposit::destroy_contract(contract_account)?;

			// this is needed to:
			// 1) Send any balance that was send to the contract after termination.
			// 2) To fail termination if any locks or holds prevent to completely empty the account.
			let balance = <Contracts<T>>::convert_native_to_evm(<AccountInfo<T>>::total_balance(
				contract_address.into(),
			));
			Self::transfer(
				&origin,
				contract_account,
				&args.beneficiary,
				balance,
				Preservation::Expendable,
				transaction_meter,
				exec_config,
			)?;
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

**File:** substrate/frame/revive/src/deposit_payment.rs (L421-440)
```rust
	/// Refunds the full native hold to `dst` ignoring the per-contributor cap, then settles the
	/// PGAS hold via [`Self::settle_pgas_refund`] (refunding `RefundPercent` to `dst` and burning
	/// the rest). The native cap only makes sense for partial refunds on a live contract; at
	/// termination there is one recipient and the contract is gone.
	///
	/// Note: callers must run inside a storage layer so partial state rolls back on error.
	fn refund_all(
		from: &T::AccountId,
		dst: Funds<T::AccountId>,
	) -> Result<BalanceOf<T>, DispatchError> {
		let to = match &dst {
			Funds::Balance(to) | Funds::TxFee(to) => *to,
		};
		let native = <() as Deposit<T>>::refund_all(from, dst)?;
		let reason = HoldReason::StorageDepositReserve;

		let pgas = Self::pgas_on_hold(reason, from);
		let pgas = Self::settle_pgas_refund(reason, from, to, pgas)?;
		Ok(native.saturating_add(pgas))
	}
```

**File:** substrate/frame/revive/src/tests/deposit_payment.rs (L460-526)
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
```
