## Analysis

The external report's core broken invariant is: **code that requires an ERC-20 `transfer`/`approve` call to return a properly ABI-encoded `bool`, and treats any successful-but-non-decodable return as a failure — even though the underlying token balance already moved.** The strongest local analog to this pattern lives in Asset Hub's `ERC20Transactor`, which bridges `pallet-revive` (EVM) ERC-20 tokens into the XCM asset-holding model. [1](#0-0) 

### Title
Non-standard-compliant ERC-20 `transfer` return data causes permanent loss of withdrawn tokens in `ERC20Transactor::withdraw_asset_with_surplus` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` moves a user's `pallet-revive` ERC-20 tokens to a runtime-controlled `TransfersCheckingAccount` via a real `IERC20::transfer` contract call, and only credits the equivalent value into the XCM holding register if the contract's return data ABI-decodes to `true`. If the call succeeds (does not revert, i.e. the token balance really moves) but the return data cannot be ABI-decoded as a `bool` — exactly the class of non-standard ERC-20 behavior described in the external USDT report — the function returns `Err(XcmError::FailedToTransactAsset(...))` without ever producing an `AssetsInHolding` credit for the withdrawn amount.

### Finding Description
`withdraw_asset_with_surplus` calls the target contract's `transfer` function via `pallet_revive::Pallet::<T>::bare_call` and inspects the result: [2](#0-1) 

If `return_value.did_revert()` is `false` (the EVM call succeeded, meaning the token's internal ledger was actually updated and the tokens were really moved to `checking_address`) but `IERC20::transferCall::abi_decode_returns_validate` fails to decode the return data as `bool` (empty/short/non-standard return data), the `?` operator immediately propagates `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`. No `AssetsInHolding` value is ever constructed for this branch.

Because `pallet_xcm::execute` (and message-queue XCM execution generally) does **not** roll back state on partial/incomplete XCM outcomes — it treats `Outcome::Incomplete` as a successful dispatch and only traps whatever assets happen to still be in the holding register — the already-executed, permanent token transfer to `TransfersCheckingAccount` is retained even though the `WithdrawAsset` instruction as a whole errors out. This is explicitly demonstrated by the pallet's own test suite comment: "Even though assets are trapped, the extrinsic returns success." [3](#0-2) 

Since `withdraw_asset_with_surplus` errors *before* constructing any `AssetsInHolding` credit for the asset, there is nothing in the holding register to trap for this asset via the `DropAssets`/`AssetTraps` mechanism either — the value simply disappears from the user's control into the checking account with no on-chain accounting path back to them.

### Impact Explanation
Any ERC-20 token registered for use with `ERC20Transactor` (e.g. via the erc20-as-XCM-asset integration visible in `asset-hub-westend` tests) whose `transfer` implementation is a legitimate but non-standard-compliant token (returns no data, or non-boolean data, on success — the exact USDT-class behavior called out in the external report) causes any user withdrawing that asset through this transactor to have their real token balance moved to the runtime's checking account permanently, with zero corresponding credit anywhere in the XCM system and no trap/claim recovery path. This is a direct, unprivileged **permanent user-fund lock**, matching the accepted impact category.

### Likelihood Explanation
No malicious peer, relayer, validator, collator, or governance action is required — only an unprivileged user constructing an ordinary `WithdrawAsset` XCM (e.g. via `pallet_xcm::execute`, as already exercised by the repository's own `smart_contract_does_not_return_bool_fails` test) against a registered ERC-20 asset whose contract legitimately succeeds but doesn't return an ABI-decodable `bool`. The repository's existing test only asserts that overall execution `is_err()`; it does not verify that the already-transferred tokens are actually recoverable, and per the confirmed pallet_xcm semantics, they are not. [4](#0-3) 

### Recommendation
Do not conflate "call reverted" with "return data undecodable." For the case where `did_revert()` is `false` but the return data cannot be ABI-decoded as `bool`, either treat this the same as a successful `true` (per common "loose" ERC-20 handling used by `SafeERC20`, since the report's own recommendation is to use `forceApprove`/similar tolerant semantics), or — if strict standard compliance is required — avoid initiating a real, irreversible transfer speculatively before confirming the decode will succeed, or add a compensating mechanism (retry/refund transfer back to the withdrawer) when the decode fails, so that the checking-account transfer is never left uncredited and unrecoverable.

### Proof of Concept
1. Deploy (or register as an XCM asset) a `pallet-revive` ERC-20-like contract whose `transfer(address,uint256)` performs a real balance-ledger update but returns no data (or non-boolean data) — mirroring real-world USDT-class token behavior.
2. As any unprivileged user holding balance of this token, submit `pallet_xcm::execute` with a `WithdrawAsset` instruction for this asset (as in the existing `smart_contract_does_not_return_bool_fails` test at `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs:2022-2073`, but using a contract that returns empty/invalid data instead of a `uint256`).
3. Observe: the extrinsic dispatch itself succeeds (per `pallet_xcm` semantics shown in `claim_assets_works`), the ERC-20 `transfer` to `TransfersCheckingAccount` has actually executed (checking account's real balance increased, user's real balance decreased), yet the XCM `Outcome` is `Incomplete`/errored with `FailedToTransactAsset`, and no `AssetsTrapped` event nor holding credit exists for the withdrawn amount — the user's tokens are unrecoverably stuck in the checking account.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-216)
```rust
	fn withdraw_asset_with_surplus(
		what: &Asset,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(AssetsInHolding, Weight), XcmError> {
		tracing::trace!(
			target: "xcm::transactor::erc20::withdraw",
			?what, ?who,
		);
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
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err(XcmError::FailedToTransactAsset("ERC20 contract execution errored"))
		}
	}
```

**File:** polkadot/xcm/pallet-xcm/src/tests/mod.rs (L720-734)
```rust
// Like `trapped_assets_can_be_claimed` but using the `claim_assets` extrinsic.
#[test]
fn claim_assets_works() {
	let balances = vec![(ALICE, INITIAL_BALANCE)];
	new_test_ext_with_balances(balances).execute_with(|| {
		// First trap some assets.
		let trapping_program =
			Xcm::<RuntimeCall>::builder_unsafe().withdraw_asset((Here, SEND_AMOUNT)).build();
		// Even though assets are trapped, the extrinsic returns success.
		assert_ok!(XcmPallet::execute(
			RuntimeOrigin::signed(ALICE),
			Box::new(VersionedXcm::from(trapping_program)),
			BaseXcmWeight::get() * 2,
		));
		assert_eq!(Balances::total_balance(&ALICE), INITIAL_BALANCE - SEND_AMOUNT);
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2055-2073)
```rust
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
