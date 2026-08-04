## Analysis

The external report's core issue — "not all ERC20-like tokens follow the standard behavior, and there's no automated way to detect this before trusting a token 1:1" — has a direct, structurally identical analog in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`. `ERC20Transactor` lets Asset Hub treat *any* deployed `pallet-revive` smart contract addressed via `AccountKey20` as an ERC20 reserve asset for XCM, without verifying that the contract's `transfer` semantics actually preserve amount 1:1.

### Title
Fee-on-transfer / non-conforming ERC20 tokens let `ERC20Transactor` credit XCM holding with more value than is actually received by the shared checking account, under-collateralizing reserve-backed transfers - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` calls the ERC20 contract's `transfer(checking_account, amount)` and, if the boolean return decodes to `true`, unconditionally credits the XCM holding register with the full requested `amount` via `AssetsInHolding::new_from_fungible_credit(..., Erc20Credit(amount))`. It never checks the actual balance change of `ERC20TransfersCheckingAccount`. Any ERC20 contract that charges a transfer fee, burns part of the transferred amount, or otherwise delivers less than `amount` while still returning `true` will cause the recorded XCM asset value to exceed what the checking account truly received.

### Finding Description
`Matcher::matches_fungibles(what)` only maps a `Location`/`AccountKey20` to an arbitrary contract address and amount — there is no allow-list or conformance check on the contract's behavior [1](#0-0) . The withdraw path only inspects `did_revert()` and the ABI-decoded boolean return of `transfer`, then blindly credits `amount`: [2](#0-1) 

The symmetric deposit path has the same trust assumption: it calls `transfer` from the shared `TransfersCheckingAccount` for the full `amount` and treats a `true` return as complete success, again without confirming the beneficiary actually received `amount`: [3](#0-2) 

The `ERC20TransfersCheckingAccount` is a single shared, chain-level account for all cross-chain movement of a given ERC20 asset [4](#0-3) . The existing test suite only checks that a *non-ERC20* contract or one returning a non-boolean type causes a hard failure [5](#0-4) [6](#0-5) ; there is no test or guard for a contract that *does* conform to the ABI/return-`true` contract but silently moves fewer tokens (fee-on-transfer, deflationary, or reflection tokens) — exactly the class of "extension standard / non-standard token" issue flagged in the external report.

### Impact Explanation
Because the credited `Erc20Credit(amount)` in the XCM holding register feeds directly into reserve-based cross-chain accounting (e.g., `ReserveAssetDeposited`/teleport-style messages telling a remote chain "N units are now backed on Asset Hub"), a fee-on-transfer ERC20 lets Asset Hub assert to the rest of the network that the checking account holds more of the token than it actually does. This is an unbacked-mint/under-collateralization condition: subsequent legitimate `deposit_asset_with_surplus` calls for other users of the same asset will eventually fail because the checking account's real ERC20 balance is short of the sum of amounts previously credited, causing denial of service and, more importantly, breaking the 1:1 reserve backing that other chains rely on when accepting AH-issued representations of the asset. This falls squarely in the "theft or unbacked mint or unlock" / "permanent user-fund or bridge-state lock" impact categories, and requires no privileged actor — any user can deploy or use an already-deployed standard-looking ERC20 with a transfer fee.

### Likelihood Explanation
No malicious peer, validator, collator, or governance action is needed. Any unprivileged user can (a) create/deploy a fee-on-transfer ERC20 via `pallet-revive` (permissionless contract deployment) and (b) initiate an XCM `WithdrawAsset`/`DepositAsset` referencing that contract as the reserve asset, since `ERC20Matcher` accepts any `AccountKey20` address without an integration checklist or allow-list. Fee-on-transfer/deflationary token designs are common in the wider EVM ecosystem, making this a realistic, not merely theoretical, class of ERC20.

### Recommendation
`withdraw_asset_with_surplus` and `deposit_asset_with_surplus` should read the checking/beneficiary account's actual ERC20 balance (via a `balanceOf` call) before and after the `transfer` call and credit/require exactly the observed delta rather than trusting the requested `amount` and boolean return value. Alternatively (matching the spirit of the external report's recommendation), maintain and enforce an allow-list ("Token Integration Checklist" equivalent) of ERC20 contracts verified to be balance-conserving (no transfer fee, no rebasing, no reentrant hooks) before they can be registered as XCM reserve assets through `ERC20Matcher`.

### Proof of Concept
1. Deploy a minimal ERC20-compatible contract via `pallet-revive` whose `transfer(to, amount)` moves `amount * 95 / 100` to `to`, burns the remainder, and returns `true` (a standard fee-on-transfer pattern).
2. From a funded account, submit an XCM program: `WithdrawAsset` for `100` units of that contract (via `AccountKey20`), `BuyExecution`, `DepositReserveAsset`/`InitiateReserveWithdraw` to a remote chain, `SetTopic`.
3. `ERC20Transactor::withdraw_asset_with_surplus` invokes `transfer(checking_account, 100)`; the contract delivers only `95` to `ERC20TransfersCheckingAccount` but returns `true`, so the executor credits `Erc20Credit(100)` and forwards a reserve message claiming `100` units are backed.
4. Repeat with several users; the checking account's true ERC20 balance falls further behind the sum of amounts XCM has told remote chains are backed.
5. Have another legitimate user attempt `deposit_asset_with_surplus` for an amount that, combined with prior shortfalls, exceeds the checking account's actual balance — the `transfer` call fails/reverts, demonstrating the accounting break and resultant asset lock/failed settlement, while remote chains still hold representations implying full backing.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-164)
```rust
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
		let who = AccountIdConverter::convert_location(who)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		// We need to map the 32 byte checking account to a 20 byte account.
		let checking_account_eth = T::AddressMapper::to_address(&TransfersCheckingAccount::get());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-299)
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
		} else {
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-237)
```rust
parameter_types! {
	/// Taken from the real gas and deposits of a standard ERC20 transfer call.
	pub const ERC20TransferGasLimit: Weight = Weight::from_parts(500_000_000_000, 10 * 1024 * 1024);
	pub const ERC20TransferStorageDepositLimit: Balance = 10_200_000_000;
	pub ERC20TransfersCheckingAccount: AccountId = PalletId(*b"py/revch").into_account_truncating();
	pub DapBufferAccount: AccountId = pallet_dap::Pallet::<Runtime>::buffer_account();
}

/// Transactor for ERC20 tokens.
pub type ERC20Transactor = assets_common::ERC20Transactor<
	// We need this for accessing pallet-revive.
	Runtime,
	// The matcher for smart contracts.
	assets_common::ERC20Matcher,
	// How to convert from a location to an account id.
	LocationToAccountId,
	// The maximum gas that can be used by a standard ERC20 transfer.
	ERC20TransferGasLimit,
	// The maximum storage deposit that can be used by a standard ERC20 transfer.
	ERC20TransferStorageDepositLimit,
	// We're generic over this so we can't escape specifying it.
	AccountId,
	// Checking account for ERC20 transfers.
	ERC20TransfersCheckingAccount,
>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1971-2016)
```rust
#[test]
fn smart_contract_not_erc20_will_error() {
	let sender: AccountId = ALICE.into();
	let beneficiary: AccountId = BOB.into();
	let revive_account = pallet_revive::Pallet::<Runtime>::account_id();
	let checking_account =
		asset_hub_westend_runtime::xcm_config::ERC20TransfersCheckingAccount::get();
	let initial_wnd_amount = 10_000_000_000_000u128;

	ExtBuilder::<Runtime>::default().build().execute_with(|| {
		// Bring the revive account to life.
		assert_ok!(Balances::mint_into(&revive_account, initial_wnd_amount));

		// Fund all accounts involved.
		assert_ok!(Balances::mint_into(&sender, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&beneficiary, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&checking_account, initial_wnd_amount));

		let (code, _) = compile_module("dummy").unwrap();

		let Contract { addr: non_erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
			.build_and_unwrap_contract();

		let wnd_amount_for_fees = 1_000_000_000_000u128;
		let erc20_transfer_amount = 100u128;
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: non_erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.build();
		// Execution fails but doesn't panic.
		assert!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(2_500_000_000, 120_000),
		)
		.is_err());
	});
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2019-2073)
```rust
// Here the contract returns a number but because it can be cast to true
// it still succeeds.
#[test]
fn smart_contract_does_not_return_bool_fails() {
	let sender: AccountId = ALICE.into();
	let beneficiary: AccountId = BOB.into();
	let revive_account = pallet_revive::Pallet::<Runtime>::account_id();
	let checking_account =
		asset_hub_westend_runtime::xcm_config::ERC20TransfersCheckingAccount::get();
	let initial_wnd_amount = 10_000_000_000_000u128;

	ExtBuilder::<Runtime>::default().build().execute_with(|| {
		// Bring the revive account to life.
		assert_ok!(Balances::mint_into(&revive_account, initial_wnd_amount));

		// Fund all accounts involved.
		assert_ok!(Balances::mint_into(&sender, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&beneficiary, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&checking_account, initial_wnd_amount));

		// This contract implements the ERC20 interface for `transfer` except it returns a uint256.
		let code = compile_module_with_type("MyTokenFake", FixtureType::Resolc)
			.expect("compile ERC20")
			.0;

		let initial_amount_u256 = U256::from(1_000_000_000_000u128);
		let constructor_data = sol_data::Uint::<256>::abi_encode(&initial_amount_u256);

		let Contract { addr: non_erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
			.data(constructor_data)
			.build_and_unwrap_contract();

		let wnd_amount_for_fees = 1_000_000_000_000u128;
		let erc20_transfer_amount = 100u128;
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: non_erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.build();
		// Execution fails but doesn't panic.
		assert!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(2_500_000_000, 220_000),
		)
		.is_err());
	});
```
