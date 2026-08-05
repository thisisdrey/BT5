## Analog Found: Unverified balance-delta trust in `ERC20Transactor` (pallet-revive XCM asset transactor)

The Illuminate bug's core invariant break is: **an amount is credited/charged based on a nominal value, while the actual balance movement is determined by an attacker-controlled, arbitrary token contract that is never checked against that nominal value.** The same pattern exists in this repo's `ERC20Transactor`, which lets XCM's asset-holding accounting be driven entirely by a boolean return value from a user-deployed, fully-arbitrary `pallet-revive` (EVM) contract instead of a verified `balanceOf` delta.

### Title
Unbacked XCM asset credit via return-value-only trust in `ERC20Transactor` withdraw/deposit for arbitrary revive contracts - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` credit/debit `AssetsInHolding` using the XCM-declared `amount` and only the *boolean success* of a `transfer()` call into an arbitrary, permissionlessly-deployed `pallet-revive` contract. Neither function verifies the actual token balance delta (`balanceOf` before/after) of the checking account or beneficiary. Any unprivileged user can deploy a malicious "ERC20" contract (full EVM bytecode is allowed, `AllowEVMBytecode: ConstBool<true>`, `substrate/bin/node/runtime/src/lib.rs:1634`) whose `transfer()` always returns `true` while moving an amount different from (or unrelated to) the requested `amount`. This decouples the nominal XCM asset amount from real token movement, exactly like the ERC777 hook decoupling fee-bearing "lent" amount from the actual "premium" swapped.

### Finding Description
`Matcher::matches_fungibles(what)` extracts only the *declared* `amount` from the XCM `Asset` — it does not inspect real contract state: [1](#0-0) 

The withdraw path calls `transfer(checking_address, amount)` and, if the call doesn't revert and the decoded return is `true`, unconditionally mints an `AssetsInHolding` credit of exactly `amount` — with no check that the checking account's real balance increased by `amount` (or at all): [2](#0-1) 

The deposit path mirrors this: it calls `transfer(beneficiary, amount)` from the checking account and, again, trusts only the decoded boolean, crediting the beneficiary's on-chain (contract-internal) balance with `amount` regardless of whether the checking account's balance was ever actually debited by that much: [3](#0-2) 

Both `balance`/`total_issuance`/`reducible_balance` used elsewhere in the pallet also just relay whatever the arbitrary contract self-reports via `balanceOf`, with zero invariant checks: [4](#0-3) 

Because `pallet-revive` permits arbitrary Solidity/EVM contracts to be deployed by any unprivileged account (proven by the `MyToken`/`Resolc`-compiled fixture used in existing tests), and because `ERC20Matcher` accepts any `AccountKey20` address as a valid XCM asset once code exists there, an attacker fully controls the semantics of `transfer()`. They can implement a contract where `transfer()` unconditionally returns `true` while performing an arbitrary (including zero, or larger-than-requested) internal balance change — the exact ERC777-hook analog: the protocol-level amount used for XCM accounting (`amount`) is disconnected from the real balance delta that the contract chooses to apply.

### Impact Explanation
This breaks the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant for XCM asset transactors: withdraw/deposit success is asserted purely on a contract's self-reported boolean, not on settlement. This enables:
- Crediting `AssetsInHolding` with `amount` that was never actually moved into the checking account (fabricated deposits within the XCM executor's accounting for that asset).
- Beneficiaries receiving contract-internal balance increases via `deposit_asset_with_surplus` without the checking account having been genuinely debited that amount.
- If such an ERC20 asset is later paired with real-value assets (e.g., an `AssetConversion` pool with native WND, since the same trust-the-return pattern underlies `impl_fungibles.rs` transfer/balance calls used by pool operations), the fabricated/unbacked balance can be used to drain real backing assets from counterparties — an unbacked-mint / theft-of-real-value scenario, which is explicitly in scope ("theft or unbacked mint or unlock").

### Likelihood Explanation
No privileged actor, validator, collator, or relayer collusion is required. Any user can permissionlessly:
1. Deploy an arbitrary contract via `pallet_revive::Pallet::<Runtime>::instantiate` (as shown in the existing `withdraw_and_deposit_erc20s` test flow using `bare_instantiate`).
2. Reference that contract's `H160` address as an `AccountKey20` XCM asset location, which `ERC20Matcher`/`ERC20Transactor` will pick up automatically.
3. Submit `PolkadotXcm::execute` with `WithdrawAsset`/`DepositAsset` instructions referencing that asset — exactly the flow already exercised in `withdraw_and_deposit_erc20s`: [5](#0-4) 

This is a public, unprivileged entrypoint (`pallet_xcm::execute` / any XCM program routing through this transactor), matching the accepted "public underpriced work" / "unauthorized ... theft" impact categories, with the sole prerequisite being deployment of a normal (if malicious) smart contract — not a privileged or infrastructure compromise.

### Recommendation
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` should snapshot `balanceOf` (or the checking/beneficiary account balance) before and after the `transfer` call and only credit/debit `AssetsInHolding` based on the *measured delta*, reverting or failing the XCM instruction if the delta does not equal the requested `amount`. This mirrors the recommended Illuminate fix ("charge/credit based on the actual balance after execution, not the nominal declared amount").

### Proof of Concept
1. Attacker deploys (via `pallet_revive::Pallet::<Runtime>::instantiate`, permissionless) a contract `EvilToken` implementing `IERC20` where `transfer(address,uint256)` always returns `true` and either performs no real balance mutation, or mutates a different amount than requested.
2. Attacker submits an XCM program via `PolkadotXcm::execute`:
   ```
   WithdrawAsset (AccountKey20{EvilToken}, X)
   DepositAsset  (AllCounted(1), beneficiary)
   ```
   following the same shape as the existing `withdraw_and_deposit_erc20s` test at [6](#0-5) .
3. `withdraw_asset_with_surplus` calls `EvilToken.transfer(checking_address, X)`, gets `true` back, and credits `AssetsInHolding` with `X` regardless of whether `checking_address`'s real (contract-tracked) balance changed at all.
4. `deposit_asset_with_surplus` calls `EvilToken.transfer(beneficiary, X)` from the checking account, again trusting only the `true` return, crediting the beneficiary with `X` even though the checking account was never actually debited that amount.
5. Result: `X` units of "ERC20 balance" exist for `beneficiary` with no corresponding real transfer having occurred through the checking account — the XCM accounting and the contract's real state have diverged, and this divergence can be chained into any downstream logic (e.g., liquidity pools) that trusts the checking-account/beneficiary balance as genuine backing.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-169)
```rust
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
		let who = AccountIdConverter::convert_location(who)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		// We need to map the 32 byte checking account to a 20 byte account.
		let checking_account_eth = T::AddressMapper::to_address(&TransfersCheckingAccount::get());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let weight_limit = WeightLimit::get();
		// To withdraw, we actually transfer to the checking account.
		// We do this using the solidity ERC20 interface.
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-208)
```rust
		if let Ok(return_value) = result {
			tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?return_value, "Return value by withdraw_asset");
			if return_value.did_revert() {
				tracing::debug!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract reverted");
				Err(XcmError::FailedToTransactAsset("ERC20 contract reverted"))
			} else {
				let is_success = IERC20::transferCall::abi_decode_returns_validate(&return_value.data).map_err(|error| {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?error, "ERC20 contract result couldn't decode");
					XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")
				})?;
				if is_success {
					tracing::trace!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract was successful");
					Ok((
						AssetsInHolding::new_from_fungible_credit(
							what.id.clone(),
							Box::new(Erc20Credit(amount)),
						),
						surplus,
					))
				} else {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", "contract transfer failed");
					Err(XcmError::FailedToTransactAsset("ERC20 contract transfer failed"))
				}
			}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-298)
```rust
		let data = IERC20::transferCall { to: address, value: EU256::from(amount) }.abi_encode();
		let weight_limit = WeightLimit::get();
		let ContractResult { result, weight_consumed, storage_deposit, .. } =
			pallet_revive::Pallet::<T>::bare_call(
				OriginFor::<T>::signed(TransfersCheckingAccount::get()),
				asset_contract_id,
				U256::zero(),
				TransactionLimits::WeightAndDeposit {
					weight_limit,
					deposit_limit: StorageDepositLimit::get(),
				},
				data,
				&ExecConfig::new_substrate_tx(),
			);
		// We need to return this surplus for the executor to allow refunding it.
		let surplus = weight_limit.saturating_sub(weight_consumed);
		tracing::trace!(target: "xcm::transactor::erc20::deposit", ?weight_consumed, ?surplus, ?storage_deposit);
		if let Ok(return_value) = result {
			tracing::trace!(target: "xcm::transactor::erc20::deposit", ?return_value, "Return value");
			if return_value.did_revert() {
				tracing::debug!(target: "xcm::transactor::erc20::deposit", "Contract reverted");
				Err((what, XcmError::FailedToTransactAsset("ERC20 contract reverted")))
			} else {
				match IERC20::transferCall::abi_decode_returns_validate(&return_value.data) {
					Ok(true) => {
						tracing::trace!(target: "xcm::transactor::erc20::deposit", "ERC20 contract was successful");
						Ok(surplus)
					},
					Ok(false) => {
						tracing::debug!(target: "xcm::transactor::erc20::deposit", "contract transfer failed");
						Err((
							what,
							XcmError::FailedToTransactAsset("ERC20 contract transfer failed"),
						))
					},
					Err(error) => {
						tracing::debug!(target: "xcm::transactor::erc20::deposit", ?error, "ERC20 contract result couldn't decode");
						Err((
							what,
							XcmError::FailedToTransactAsset(
								"ERC20 contract result couldn't decode",
							),
						))
					},
				}
			}
```

**File:** substrate/frame/revive/src/impl_fungibles.rs (L94-134)
```rust
	fn total_balance(asset_id: Self::AssetId, account_id: &T::AccountId) -> Self::Balance {
		// Since ERC20s don't have the concept of freezes and locks,
		// total balance is the same as balance.
		Self::balance(asset_id, account_id)
	}

	fn balance(asset_id: Self::AssetId, account_id: &T::AccountId) -> Self::Balance {
		let eth_address = T::AddressMapper::to_address(account_id);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
		let data = IERC20::balanceOfCall { account: address }.abi_encode();
		let ContractResult { result, .. } = Self::bare_call(
			OriginFor::<T>::signed(account_id.clone()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
		if let Ok(return_value) = result &&
			let Ok(eu256) = EU256::abi_decode_validate(&return_value.data)
		{
			eu256.to::<u128>()
		} else {
			0
		}
	}

	fn reducible_balance(
		asset_id: Self::AssetId,
		account_id: &T::AccountId,
		_: Preservation,
		_: Fortitude,
	) -> Self::Balance {
		// Since ERC20s don't have minimum amounts, this is the same
		// as balance.
		Self::balance(asset_id, account_id)
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1864-1929)
```rust
#[test]
fn withdraw_and_deposit_erc20s() {
	let sender: AccountId = ALICE.into();
	let beneficiary: AccountId = BOB.into();
	let revive_account = pallet_revive::Pallet::<Runtime>::account_id();
	let checking_account =
		asset_hub_westend_runtime::xcm_config::ERC20TransfersCheckingAccount::get();
	let initial_wnd_amount = 100_000_000_000_000_000u128;
	sp_tracing::init_for_tests();

	ExtBuilder::<Runtime>::default().build().execute_with(|| {
		// Bring the revive account to life.
		assert_ok!(Balances::mint_into(&revive_account, initial_wnd_amount));
		// Fund all accounts involved.
		assert_ok!(Balances::mint_into(&sender, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&beneficiary, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&checking_account, initial_wnd_amount));

		let code = compile_module_with_type("MyToken", FixtureType::Resolc)
			.expect("compile ERC20")
			.0;

		let initial_amount_u256 = U256::from(1_000_000_000_000u128);
		let constructor_data = sol_data::Uint::<256>::abi_encode(&initial_amount_u256);
		let Contract { addr: erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
			.data(constructor_data)
			.build_and_unwrap_contract();

		let sender_balance_before = <Balances as fungible::Inspect<_>>::balance(&sender);

		let erc20_transfer_amount = 100u128;
		let wnd_amount_for_fees = 10_000_000_000_000u128;
		// Actual XCM to execute locally.
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.refund_surplus()
			.deposit_asset(AllCounted(1), sender.clone())
			.build();
		assert_ok!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(600_000_000_000, 15 * 1024 * 1024),
		));

		// Revive is not taking any fees.
		let sender_balance_after = <Balances as fungible::Inspect<_>>::balance(&sender);
		// Balance after is larger than the difference between balance before and transferred
		// amount because of the refund.
		assert!(sender_balance_after > sender_balance_before - wnd_amount_for_fees);

		// Beneficiary receives the ERC20.
		let beneficiary_amount =
			<Revive as fungibles::Inspect<_>>::balance(erc20_address, &beneficiary);
		assert_eq!(beneficiary_amount, erc20_transfer_amount);
	});
}
```
