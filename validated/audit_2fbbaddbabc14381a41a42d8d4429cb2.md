This confirms the exploit path is fully reachable without any privileged action. `ERC20Matcher` is defined as `MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>`, which matches *any* location of the form `AccountKey20 { key, .. }` — meaning any contract address is treated as a valid ERC20 asset id, with no registration or governance step required. [1](#0-0)  Any unprivileged user can call `pallet_revive`'s public instantiate extrinsic to deploy an arbitrary contract implementing `IERC20`, then reference that contract's address directly in an XCM program's `WithdrawAsset`/`DepositAsset` — confirmed by the test `withdraw_and_deposit_erc20s`, which instantiates a token via `bare_instantiate` and immediately uses its address in a `WithdrawAsset`/`DepositAsset` XCM without any registration step. [2](#0-1) 

The transactor code matches the claim precisely: `withdraw_asset_with_surplus` credits `AssetsInHolding` with the full nominal `Erc20Credit(amount)` purely based on the decoded boolean return of `transferCall`, never checking actual balance movement. [3](#0-2)  `deposit_asset_with_surplus` similarly trusts `Ok(true)` unconditionally. [4](#0-3)  The existing test suite only covers reverts and non-boolean returns (`smart_contract_not_erc20_will_error`, `smart_contract_does_not_return_bool_fails`), confirming there is no balance-delta verification and no fee-on-transfer/rebasing-token test coverage. [5](#0-4) 

This satisfies an unprivileged, public-entrypoint attack path (self-deployed contract + standard XCM execute), matches the code exactly as cited, and results in the XCM holding register (and hence subsequent `DepositAsset`/`BuyExecution` accounting) diverging from the real balance actually moved in/out of the shared `TransfersCheckingAccount` — a theft/unbacked-value-creation class issue.

Audit Report

## Title
`ERC20Transactor` trusts nominal `transfer()` boolean return without verifying actual balance delta, letting fee-on-transfer/rebasing ERC20 tokens desynchronize the XCM holding register from real checking-account balance - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `::deposit_asset_with_surplus` move ERC20 value via `pallet_revive::Pallet::<T>::bare_call` invoking Solidity `IERC20::transfer()`, then credit/debit the XCM `AssetsInHolding` register with the *requested* `amount` whenever the ABI-decoded return is `true`, without ever measuring the actual balance change of the recipient. Any unprivileged user can deploy a `pallet_revive` contract implementing a custom `IERC20::transfer` that returns `true` while moving less than `amount` (fee-on-transfer/rebasing token), and because `ERC20Matcher` accepts any `AccountKey20` location as a valid asset with no registration step, that contract can immediately be referenced in an XCM program, causing the shared `TransfersCheckingAccount` to receive less value than the holding register believes it credited.

## Finding Description
`withdraw_asset_with_surplus` calls `IERC20::transferCall` to move `amount` from the XCM origin to `TransfersCheckingAccount`, then on `is_success == true` unconditionally credits `AssetsInHolding` with `Erc20Credit(amount)` — the nominal requested amount, not an observed delta. [3](#0-2)  Symmetrically, `deposit_asset_with_surplus` debits `amount` from holding and transfers from `TransfersCheckingAccount` to the beneficiary, again trusting the boolean return. [6](#0-5) 

The asset-matching layer used by this transactor, `ERC20Matcher`, matches *any* local `AccountKey20`-junction location as a valid fungible ERC20 asset id with no allowlist or governance-gated registration. [7](#0-6)  This means any account can call `pallet_revive`'s public instantiate path to deploy a hostile ERC20 contract and immediately use its address in an XCM `WithdrawAsset`/`DepositAsset` program, as demonstrated by the existing test `withdraw_and_deposit_erc20s` which does exactly that with a compliant token. [8](#0-7)  The existing guards (`did_revert()` check and `abi_decode_returns_validate` for `true`/`false`) only reject a hard revert or explicit `false`; they never verify that the recipient's real balance changed by `amount`, as confirmed by the only negative-path tests in the suite (`smart_contract_not_erc20_will_error`, `smart_contract_does_not_return_bool_fails`), neither of which covers a token that returns `true` while transferring a smaller amount. [5](#0-4) 

## Impact Explanation
The shared `TransfersCheckingAccount` custodies all ERC20 value that the XCM holding register represents on this transactor. A fee-on-transfer/deflationary token can report success while under-delivering value on withdraw, causing the register to be credited with more than the checking account actually received; this fabricated credit is then usable within the same XCM program (`DepositAsset`, `BuyExecution`, onward transfer) to move out more real value than was actually locked in. This is an unbacked-value-creation / theft-class impact tied to the exact corrupted value: the `amount` field credited into `AssetsInHolding` via `Erc20Credit(amount)` at `erc20_transactor.rs:198-201`, which diverges from the real balance delta in `TransfersCheckingAccount`.

## Likelihood Explanation
The attacker needs only to deploy a self-authored `pallet_revive` contract implementing `IERC20` with custom `transfer` semantics and reference its address in a standard `pallet_xcm::execute`/XCM program — both fully public, unprivileged actions, with no governance, registration, relayer, or validator dependency, as confirmed by the matcher's unconditional acceptance of any `AccountKey20` location and by existing tests exercising this exact contract-deploy-then-XCM-use flow.

## Recommendation
Replace trust in the ABI-decoded boolean with an observed balance-delta check: read `balanceOf` of the recipient (`TransfersCheckingAccount` on withdraw, the beneficiary on deposit) before and after the `bare_call`, and credit/debit `AssetsInHolding` with the observed delta rather than the nominal `amount`. If the observed delta differs from the requested `amount`, fail with `FailedToTransactAsset` rather than silently crediting/debiting a mismatched value.

## Proof of Concept
1. Deploy a `pallet_revive` contract implementing `IERC20` where `transfer(to, value)` moves `value * 99 / 100` to `to` but always returns `abi_encode(true)`.
2. Because `ERC20Matcher` accepts any `AccountKey20` location without registration, submit an XCM program (via `pallet_xcm::execute`, as in `withdraw_and_deposit_erc20s`) performing `WithdrawAsset` of `amount` of this token, followed by `DepositAsset`/onward transfer of the full nominal `amount` in the same program.
3. Observe `TransfersCheckingAccount`'s real ERC20 balance increases by only `amount * 99/100`, while `AssetsInHolding` is credited with the full `amount` (`erc20_transactor.rs:198-201`), and the subsequent instruction successfully moves the full nominal `amount`, fabricating 1% of value relative to what was actually collateralized.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-161)
```rust
/// `Contains<Location>` implementation that matches locations with no parents,
/// a `PalletInstance` and an `AccountKey20` junction.
pub struct IsLocalAccountKey20;
impl Contains<Location> for IsLocalAccountKey20 {
	fn contains(location: &Location) -> bool {
		matches!(location.unpack(), (0, [AccountKey20 { .. }]))
	}
}

/// Fallible converter from a location to a `H160` that matches any location ending with
/// an `AccountKey20` junction.
pub struct AccountKey20ToH160;
impl MaybeEquivalence<Location, H160> for AccountKey20ToH160 {
	fn convert(location: &Location) -> Option<H160> {
		match location.unpack() {
			(0, [AccountKey20 { key, .. }]) => Some((*key).into()),
			_ => None,
		}
	}

	fn convert_back(key: &H160) -> Option<Location> {
		Some(Location::new(0, [AccountKey20 { key: (*key).into(), network: None }]))
	}
}

/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;

```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1864-1911)
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
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1971-2017)
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
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-207)
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
