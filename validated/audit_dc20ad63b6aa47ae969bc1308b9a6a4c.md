Audit Report

## Title
`ERC20Transactor` credits XCM holding with the requested amount instead of the checking account's actual balance delta, letting non-standard (fee-on-transfer/rebasing) ERC20 tokens desynchronize the shared checking account and cause other users' deposits to revert - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` calls `IERC20::transfer(checking_address, amount)` and, as long as the call does not revert and the ABI-decoded return value is `true`, unconditionally credits the XCM holding register with `Erc20Credit(amount)` — the amount requested, not the amount the shared `TransfersCheckingAccount` actually received [1](#0-0) . `deposit_asset_with_surplus` symmetrically transfers `amount` (the value carried in the holding credit) out of the same pooled checking account without checking its live balance [2](#0-1) . Because the matcher (`ERC20Matcher`) accepts any `AccountKey20` location/contract address with no allowlist, an unprivileged user can route a fee-on-transfer or rebasing ERC20 through this transactor and desynchronize the checking account's real balance from the runtime's `Erc20Credit` bookkeeping, causing subsequent legitimate deposits for other users of the same asset to fail.

## Finding Description
The `Erc20Credit` type explicitly documents that it performs no runtime-level balance enforcement and defers to the ERC20 contract itself [3](#0-2) . In `withdraw_asset_with_surplus`, the only checks performed on the `transfer` result are `did_revert()` and the ABI-decoded boolean return value; there is no comparison of the checking account's balance before and after the call against `amount` [4](#0-3) . `deposit_asset_with_surplus` likewise blindly transfers `amount` out of `TransfersCheckingAccount` based on the value carried in `AssetsInHolding`, not the account's actual live balance [5](#0-4) .

Crucially, the matcher wired into Asset Hub Westend's runtime, `ERC20Matcher`, is defined as `MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>` [6](#0-5) , and `IsLocalAccountKey20` matches any local location consisting solely of an `AccountKey20` junction with no contract-address allowlist or registry check [7](#0-6) . This confirms that any deployed contract address — including one deliberately implementing fee-on-transfer or rebasing semantics — can be used as the ERC20 asset id in this transactor's XCM flow, since `ERC20Transactor` is wired into `AssetTransactors` alongside the other adapters with this unrestricted matcher [8](#0-7) .

The exploit flow: an attacker deploys a fee-on-transfer ERC20 whose `transfer` call returns `true` but delivers less than `amount` to `TransfersCheckingAccount` (or a rebasing token whose balance later shrinks). `withdraw_asset_with_surplus` still mints the full `Erc20Credit(amount)`. Because `TransfersCheckingAccount` is one shared pooled account across all users transacting that asset id, once its real balance falls below the sum of outstanding credits recorded by other users' pending XCM operations, subsequent `deposit_asset_with_surplus` calls for the same asset will revert or return `false`, per the existing (but insufficient) `did_revert()`/boolean-return checks.

## Impact Explanation
This is a fund-lock / denial-of-service condition on a public entry point: any user can submit `PolkadotXcm::execute` (or an incoming XCM) referencing an arbitrary ERC20 contract address routed through `ERC20Transactor`. Because `TransfersCheckingAccount` is pooled per-asset across all users, one user's use of a non-standard token can strand another, unrelated user's in-flight XCM deposit for that same asset, causing it to revert mid-program. Depending on XCM executor handling, assets that fail to deposit are typically trapped (recorded as asset traps requiring a manual `claim_assets` call) rather than outright destroyed, but this still constitutes an unintended loss-of-availability / lock condition on user funds routed through the shared checking account, matching the "permanent user-fund … lock" and "public underpriced work" impact classes for this program.

## Likelihood Explanation
No privileged action is required. The matcher (`ERC20Matcher`/`IsLocalAccountKey20`) places no restriction on which contract addresses are accepted, so any ordinary user can deploy a non-standard ERC20 contract (fee-on-transfer or rebasing) and route it through a normal `withdraw_asset`/`deposit_asset` XCM sequence, as demonstrated by the existing test harness pattern that deploys an arbitrary user-supplied contract and transacts it through `ERC20TransfersCheckingAccount` [9](#0-8) . This is a repeatable, unprivileged, public-dispatch attack path.

## Recommendation
- In `withdraw_asset_with_surplus`, snapshot the checking account's ERC20 balance before and after the `transfer` call, and credit `AssetsInHolding` with the observed delta rather than the requested `amount`.
- In `deposit_asset_with_surplus`, verify the checking account's real balance is sufficient before transferring, or fail closed rather than trusting the value carried in the holding register.
- Consider maintaining a per-asset accounting invariant (checking account balance vs. total outstanding credits) or restrict `ERC20Matcher` to a vetted allowlist of standard-compliant ERC20 contracts.

## Proof of Concept
1. Deploy a fee-on-transfer ERC20 contract where `transfer(to, amount)` returns `true` but delivers `amount - fee` to `to`.
2. User A executes an XCM `withdraw_asset` for this token; `withdraw_asset_with_surplus` credits `Erc20Credit(amount)` in holding even though `TransfersCheckingAccount`'s real balance only increased by `amount - fee`.
3. Repeat with User B to widen the real-balance vs. credited-total gap.
4. Once the checking account's real balance is smaller than the sum of credited amounts, a subsequent legitimate `deposit_asset_with_surplus` for any user's deposit of this token reverts or returns `false`, per the code paths at [10](#0-9) , leaving that user's assets trapped mid-XCM.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-79)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-208)
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
		let ContractResult { result, weight_consumed, storage_deposit, .. } =
			pallet_revive::Pallet::<T>::bare_call(
				OriginFor::<T>::signed(who.clone()),
				asset_id,
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
		tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?weight_consumed, ?surplus, ?storage_deposit);
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L248-266)
```rust
		// We need to map the 32 byte beneficiary account to a 20 byte account.
		let eth_address = T::AddressMapper::to_address(&who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
		// To deposit, we actually transfer from the checking account to the beneficiary.
		// We do this using the solidity ERC20 interface.
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-298)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-139)
```rust
/// `Contains<Location>` implementation that matches locations with no parents,
/// a `PalletInstance` and an `AccountKey20` junction.
pub struct IsLocalAccountKey20;
impl Contains<Location> for IsLocalAccountKey20 {
	fn contains(location: &Location) -> bool {
		matches!(location.unpack(), (0, [AccountKey20 { .. }]))
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L157-160)
```rust
/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L221-246)
```rust
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

/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
);
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
