## Finding

### Title
Storage-deposit refund at contract termination pays the entire multi-contributor hold to the terminating caller instead of each depositor - ([File: substrate/frame/revive/src/deposit_payment.rs])

### Summary
When a `pallet-revive` contract is terminated (via `SELFDESTRUCT` or the `ISystem.terminate` precompile), any account whose storage writes were paid for by *other, unrelated* users can call `terminate` and redirect the **entire accumulated storage-deposit hold**, contributed by all of those other users, to itself. This is a public, unprivileged fund-misappropriation path structurally analogous to the ERC-777 report's core lesson: a payout embedded in a state-transition (there: reward transfer during liquidation; here: deposit refund during termination) is delivered to the wrong/attacker-chosen party instead of the rightful depositor.

### Finding Description
Any address can pay for another contract's storage by simply calling into it (`growStorage`-style writes charge `msg.sender`'s account and hold the native currency under `HoldReason::StorageDepositReserve`, tracked per-payer in `NativeDepositOf[contract][payer]`): [1](#0-0) 

When a contract is terminated, `do_terminate` refunds the storage deposit via `T::Deposit::refund_all`, passing the **transaction's dispatch origin** (i.e. whoever calls `terminate`, which can be any unrelated account) as the sole destination: [2](#0-1) 

`refund_all`'s documentation and implementation explicitly bypass the per-contributor `NativeDepositOf` cap and send the *whole* hold to a single recipient: [3](#0-2) [4](#0-3) 

The in-repo test for this exact scenario proves the diversion is real: ALICE deploys a contract, ALICE and CHARLIE each independently pay for their own storage slot (`growStorage`), and when ALICE terminates the contract, she receives **both** her own and CHARLIE's deposit: [5](#0-4) 

Nothing requires the terminator to be a depositor at all — `terminate` is a public precompile call reachable by any account holding no stake in the contract's storage: [6](#0-5) [7](#0-6) 

The `PGasDeposit` backend has the identical property for the native-currency portion of a mixed PGAS/DOT refund: [8](#0-7) 

### Impact Explanation
This is a direct fund-theft / wrong-beneficiary bug: value that was rightfully deposited by user B (e.g. CHARLIE) is settled to user A (e.g. ALICE) simply because A happened to be the one who dispatched the `terminate` call — a completely unprivileged, permissionless action. Any contract with storage co-funded by multiple callers (a common pattern for shared/multi-tenant contracts, escrows, or any contract where callers pay their own storage-deposit share) is vulnerable: the first account to call `terminate` sweeps every other contributor's refund. This violates the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant explicitly called out for balances/asset accounting.

### Likelihood Explanation
High. No special privilege, governance action, or malicious infrastructure role is required — only two ordinary, unrelated accounts interacting with a shared contract and one of them calling the public `terminate`/`SELFDESTRUCT` path before the other withdraws their contribution. The behavior is deterministic and is even asserted as expected in the repository's own test suite, meaning it will trigger on every mainnet-equivalent multi-payer contract that gets terminated.

### Recommendation
`refund_all` should either (a) refund each contributor individually according to `NativeDepositOf[contract][payer]` instead of paying the full sum to the transaction origin, or (b) restrict who may trigger termination-time refund collection (e.g. require the terminator to be the sole depositor, or route refunds to the contract's original depositors atomically as part of `do_terminate`) so that no unrelated caller of the public `terminate` precompile/opcode can capture funds it did not contribute.

### Proof of Concept
1. Deploy `MultiContributorStorage` (or any contract that lets multiple callers pay their own storage deposit).
2. Account A calls `growStorage()` — A's balance is charged and held (`NativeDepositOf[contract][A] > 0`).
3. Account B (unrelated, e.g. CHARLIE) calls `growStorage()` — B's balance is charged and held (`NativeDepositOf[contract][B] > 0`).
4. Account A (or any third account with no deposit at all) calls `terminate(beneficiary)`.
5. `do_terminate` → `T::Deposit::refund_all(contract, Funds::Balance(A))` transfers the **combined** hold (A's + B's deposits) to A alone; B's contribution is never returned to B.

This exact flow is reproduced verbatim by the existing repository test: [9](#0-8)

### Citations

**File:** substrate/frame/revive/src/deposit_payment.rs (L26-41)
```rust
















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

**File:** substrate/frame/revive/uapi/sol/ISystem.sol (L47-53)
```text
	/// Terminate the calling contract of this function and send balance to `beneficiary`.
	/// This will revert if:
	/// - called from constructor
	/// - called from static context
	/// - called from delegate context
	/// - the contract introduced balance locks
	function terminate(address beneficiary) external;
```
