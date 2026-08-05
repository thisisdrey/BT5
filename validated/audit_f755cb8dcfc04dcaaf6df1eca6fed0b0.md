This confirms all elements of the claim precisely match the actual code. `ERC20Matcher` permissionlessly matches any local `AccountKey20` location to a smart-contract address with no allow-list, via `IsLocalAccountKey20`/`AccountKey20ToH160` [1](#0-0) , and `ERC20Transactor` is wired into `AssetTransactors` for Asset Hub Westend [2](#0-1) . The `withdraw_asset_with_surplus` function credits the holding with the nominal `amount` from `Matcher::matches_fungibles`, not any measured balance delta on the checking account, only checking the boolean `transferCall` success return value [3](#0-2) . Symmetrically, `deposit_asset_with_surplus` attempts to move the same nominal `amount` out of the checking account to the beneficiary [4](#0-3) , which reverts/fails when the checking account's true balance is less than `amount` due to a fee-on-transfer token [5](#0-4) . The executor's trap mechanism confirmed by the mock test preserves the un-reconciled nominal amount in the trap rather than the real balance [6](#0-5) , meaning any later `claim_assets` retry referencing that trapped nominal amount would face the identical shortfall in the checking account and fail again, permanently locking the delta.

Audit Report

## Title
ERC20Transactor credits XCM holding with the nominal transfer amount instead of the amount actually received, causing permanent fund lock for fee-on-transfer/deflationary ERC20 tokens - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` moves ERC20 tokens to a shared checking account via `transferCall` but credits the XCM holding with the nominal requested `amount` rather than the checking account's actual received balance, with no before/after balance check. Any user-deployed fee-on-transfer or deflationary ERC20 referenced via a permissionless `AccountKey20` asset id can trigger a shortfall that causes `deposit_asset_with_surplus` to fail when it tries to move the full nominal amount out of the checking account, trapping funds that can never be fully reclaimed.

## Finding Description
`withdraw_asset_with_surplus` calls `IERC20::transferCall{to: checking_address, value: amount}` and, on a successful (non-reverted, `true`-returning) call, unconditionally constructs `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` using the nominal `amount` derived from `Matcher::matches_fungibles(what)` — never the checking account's measured balance delta. `deposit_asset_with_surplus` later calls `IERC20::transferCall{to: address, value: amount}` from the checking account using that same recorded nominal `amount`. Because `ERC20Matcher` (built from `IsLocalAccountKey20`/`AccountKey20ToH160`) permissionlessly matches any local `AccountKey20` location directly to a contract address with no registration/allow-list, and `ERC20Transactor` is included in Asset Hub Westend's `AssetTransactors`, any unprivileged user can reference their own fee-charging ERC20 contract in an XCM program executed via `pallet_xcm::execute`/`send`. If the referenced contract charges a transfer fee, the checking account actually receives less than `amount`, yet the holding still records the full nominal `amount`. When `DepositAsset` subsequently tries to move that full nominal amount out of the checking account, the ERC20 `transfer` reverts or returns `false` (insufficient balance), and the transactor returns `Err`, causing the instruction to abort and the holding (still valued at the unreconciled nominal amount) to be trapped by the executor's post-processing trap mechanism. Since the trap references the nominal, unreconciled amount and the checking account's real ERC20 balance for that shortfall never reaches it, any later `claim_assets` retry against the same recorded amount fails identically, permanently stranding the shortfall in the shared checking account.

## Impact Explanation
This is a permanent user-fund lock: the fee amount debited from the user's real ERC20 balance during `withdraw_asset_with_surplus` becomes permanently unrecoverable because the ledger entry (`Erc20Credit`/XCM holding/trap) assumes a 1:1 nominal transfer that the checking account's actual on-chain balance can never satisfy for a fee-on-transfer contract. This matches the permitted impact category "permanent user-fund ... lock" under `contracts or revive execution` and `asset accounting`, and requires no admin, relayer, validator, or malicious peer.

## Likelihood Explanation
The precondition — permissionless referencing of any `AccountKey20` contract address as an XCM asset id, with no allow-listing — is present in `ERC20Matcher`'s implementation and confirmed by the feature's own PR description. Fee-on-transfer and deflationary ERC20 semantics are legal and common under the ERC20 standard, making this a systemic gap rather than a contrived edge case, reachable by any ordinary user who deploys or references such a contract via `pallet_xcm::execute`.

## Recommendation
`withdraw_asset_with_surplus` and `deposit_asset_with_surplus` should measure the checking/beneficiary account's actual ERC20 balance before and after the `transferCall` and use the observed delta — not the nominal `amount` — when constructing/consuming `Erc20Credit`. Alternatively, explicitly reject ERC20 assets whose transfer delta does not match the requested amount (fail closed), ensuring the checking account can never be over-credited relative to its real token holdings.

## Proof of Concept
1. Deploy a standard ERC20 contract via `pallet-revive` on Asset Hub Westend that charges a transfer fee (e.g., 1%), fully valid under the ERC20 standard.
2. As any unprivileged account holding this token, call `pallet_xcm::execute` with `WithdrawAsset((AccountKey20{key: <contract>}, amount)) -> DepositAsset(All, beneficiary)`.
3. `WithdrawAsset` succeeds via `withdraw_asset_with_surplus`: `amount` is nominally credited to holding, but the checking account actually receives `amount * 0.99`.
4. `DepositAsset` invokes `deposit_asset_with_surplus`, attempting to transfer the full nominal `amount` out of the checking account; this fails/reverts since the checking account only holds `amount * 0.99`.
5. The instruction aborts; the holding (still valued at the unreconciled nominal `amount`) is trapped per the executor's trap mechanism (see `polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs`); any later `claim_assets` retry for the same amount fails identically, permanently stranding the fee shortfall in the shared checking account.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L134-160)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-203)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-266)
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

**File:** polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs (L34-71)
```rust
/// A single sub-ED deposit fails, the instruction is aborted, and the leftover holding is
/// trapped by `post_process` — funds are not lost.
#[test]
fn failed_deposit_aborts_instruction_and_post_process_traps_holding() {
	add_asset(SENDER, (Here, 1u128)); // 1 < ExistentialDeposit (=2 in mock)

	let xcm = Xcm::<TestCall>::builder_unsafe()
		.withdraw_asset((Here, 1u128))
		.deposit_asset(All, RECIPIENT)
		.build();

	let (mut vm, weight) = instantiate_executor(SENDER, xcm.clone());

	// `bench_process` returns `Err` because the retry-pass deposit failure now bubbles up.
	let result = vm.bench_process(xcm);
	let err = result.expect_err("retry-pass deposit failure must bubble up");

	// Mirror what `XcmExecutor::execute` does between `process` and `post_process`: register
	// the instruction error so `post_process` produces `Outcome::Incomplete`.
	vm.set_error(Some((err.index, err.xcm_error)));

	let outcome = vm.bench_post_process(weight);
	assert!(
		matches!(outcome, Outcome::Incomplete { .. }),
		"expected Outcome::Incomplete, got {outcome:?}"
	);

	// Recipient never received anything.
	assert!(asset_list(RECIPIENT).is_empty());

	// `post_process` trapped the holding (which `transactional_process` had restored after
	// the failed `DepositAsset`). The mock `TestAssetTrap` accumulates everything under
	// `TRAPPED_ASSETS`.
	assert_eq!(
		asset_list(TRAPPED_ASSETS),
		vec![(Here, 1u128).into()],
		"undeposited assets must be trapped, not silently lost"
	);
```
