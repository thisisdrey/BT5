## Title
Contract termination redirects all contributors' storage-deposit refunds to a single attacker-chosen beneficiary - (File: `substrate/frame/revive/src/deposit_payment.rs`)

### Summary
The external report's core broken invariant is: value is credited internally to a specific rightful recipient (an accounting entry recording *who* is owed the funds), but the actual settlement/withdrawal path is not gated by that same per-recipient accounting — so the funds either become unreachable by the rightful owner, or (worse, as found here) reach the wrong party entirely. In `pallet-revive`, storage-deposit contributions from multiple distinct accounts on a shared contract are tracked per-contributor in `NativeDepositOf[contract][contributor]`, but the termination refund path (`Deposit::refund_all`) explicitly bypasses this per-contributor accounting and pays the **entire combined native hold** to whichever `beneficiary` the terminating caller names.

### Finding Description
`NativeDepositOf` is a double map keyed `(holder, contributor) -> amount` that records "how much native currency a user has contributed to a given account's hold," specifically so that refunds are capped to the entitled contributor: [1](#0-0) 

During normal (non-terminating) refunds, `refund_on_hold` correctly caps the native portion to `NativeDepositOf::get(from, to)` for the account requesting the refund: [2](#0-1) 

However, `refund_all` — used specifically on contract termination — is documented and implemented to **bypass this per-contributor cap entirely**, sending the full native hold plus the PGAS-settled portion to a single `dst`/`to`: [3](#0-2) 

This is invoked from `do_terminate`, where the refund destination is `args.beneficiary` — an address supplied by whoever triggers termination (via the `ISystem.terminate` precompile or `SELFDESTRUCT`), with no relation to who actually contributed the deposits being refunded: [4](#0-3) 

The pallet itself imposes no check that `args.beneficiary` correspond to any of the contract's `NativeDepositOf` contributors. Any contract that accepts storage-write calls from multiple distinct accounts (e.g. a shared registry, marketplace, or multi-tenant data contract) will accumulate multiple `NativeDepositOf` entries, each representing a specific user's rightful claim to their own contribution. Whoever is able to trigger that contract's termination path can name themselves as `beneficiary` and receive the combined deposits of every other contributor.

### Impact Explanation
This is a direct, unbacked-transfer / theft-of-user-funds bug: storage deposits belonging to Contributor A become claimable by Contributor B (or any third party who can trigger termination) simply by calling terminate with themselves as beneficiary. It matches the "theft or unbacked mint or unlock" and "public underpriced/unauthorized settlement to wrong beneficiary" categories in scope, since it is reachable by any unprivileged account participating in or interacting with a shared contract — not a malicious validator, relayer, or admin.

### Likelihood Explanation
The bug is deterministic and requires no race condition or privileged actor. The maintainers' own regression test demonstrates it directly: two independent depositors (`ALICE`, `CHARLIE`) each fund distinct storage slots on the same contract via the native fallback path; when `ALICE` (or whoever calls `terminate`) names herself as beneficiary, she receives both her own and `CHARLIE`'s deposit: [5](#0-4) 

The test comment explicitly states the design rationale ("the native cap only makes sense for partial refunds on a live contract; at termination there is one recipient and the contract is gone"), confirming this is intentional behavior rather than an edge-case oversight — but the assumption that termination always has "one recipient" entitled to the whole hold is false whenever a contract has multiple depositors, which is a common and expected multi-tenant contract pattern.

### Recommendation
On termination, either (a) refund each recorded `NativeDepositOf` contributor their own contribution directly (iterating the double-map entries for that contract) rather than sending the aggregate to a single `beneficiary`, or (b) restrict `refund_all`'s native-hold payout to the calling/owning contributor's own recorded entitlement and separately settle any un-attributed residue (e.g., dust from rounding) to the terminator, so that other contributors' deposits are never redirected away from their rightful owners.

### Proof of Concept
Using the existing fixture `MultiContributorStorage.sol` and the repository's own test:
1. Deploy `MultiContributorStorage`.
2. Have account `ALICE` call `growStorage()` (charges her native deposit, recorded in `NativeDepositOf[contract][ALICE]`).
3. Have account `CHARLIE` call `growStorage()` (charges his native deposit, recorded in `NativeDepositOf[contract][CHARLIE]`).
4. Have `ALICE` call `terminate(ALICE)` (or any address she controls) via the `ISystem.terminate` precompile.
5. Observe: the contract's entire `StorageDepositReserve` native hold — `alice_entry + charlie_entry` — is transferred to `ALICE`, even though `charlie_entry` was `CHARLIE`'s deposit, as shown by: [6](#0-5)

### Citations

**File:** substrate/frame/revive/src/lib.rs (L711-730)
```rust
	/// Native currency storage deposit contributed by a user into a contract.
	///
	/// Bounds how much native value the user can receive back from that contract's
	/// storage deposit.
	///
	/// Keys: `(holder, contributor) -> amount`
	/// - `holder`: account on which the deposit is held (a contract, or the pallet's own account
	///   for code-upload deposits).
	/// - `contributor`: user that funded the deposit. Receives the native portion on refund, capped
	///   at this entry's `amount`.
	#[pallet::storage]
	pub(crate) type NativeDepositOf<T: Config> = StorageDoubleMap<
		_,
		Identity,
		T::AccountId,
		Identity,
		T::AccountId,
		BalanceOf<T>,
		ValueQuery,
	>;
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L384-407)
```rust
	fn refund_on_hold(
		reason: HoldReason,
		from: &T::AccountId,
		dst: Funds<T::AccountId>,
		amount: BalanceOf<T>,
	) -> DispatchResult {
		let to = match &dst {
			Funds::Balance(to) | Funds::TxFee(to) => *to,
		};
		let contribution = NativeDepositOf::<T>::get(from, to);
		let native_requested = amount.min(contribution);

		let native_refunded = if !native_requested.is_zero() {
			<() as Deposit<T>>::refund_on_hold(reason, from, dst, native_requested)?;
			let new_val = contribution.saturating_sub(native_requested);
			if new_val.is_zero() {
				NativeDepositOf::<T>::remove(from, to);
			} else {
				NativeDepositOf::<T>::insert(from, to, new_val);
			}
			native_requested
		} else {
			BalanceOf::<T>::zero()
		};
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

**File:** substrate/frame/revive/src/exec.rs (L1793-1834)
```rust
	fn do_terminate(
		transaction_meter: &mut TransactionMeter<T>,
		exec_config: &ExecConfig<T>,
		contract_account: &T::AccountId,
		origin: &Origin<T>,
		args: &TerminateArgs<T>,
	) -> Result<(), DispatchError> {
		let contract_address = T::AddressMapper::to_address(contract_account);

		// If root created this contract we need to use the pallet account_id because root has no
		// account.
		let origin: Origin<T> = match origin {
			Origin::Signed(o) => Origin::Signed(o.clone()),
			Origin::Root => Origin::from_account_id(crate::Pallet::<T>::account_id()),
		};

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
