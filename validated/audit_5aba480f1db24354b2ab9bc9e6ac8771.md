Audit Report

## Title
ERC20Transactor credits/debits the XCM holding register with the nominal transfer amount instead of the actually-received amount, allowing fee-on-transfer/deflationary ERC20 tokens to desynchronize the checking-account reserve from XCM asset accounting - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` drive XCM holding-register accounting for arbitrary ERC20 contracts callable through `pallet-revive`, but both only check the boolean `IERC20.transfer` return value and then unconditionally mint/burn the exact nominal `amount` — never measuring the actual balance delta of `TransfersCheckingAccount`. Because ERC20 contracts are unprivileged, user-deployable Solidity/EVM code with no allow-listing via `ERC20Matcher`, an attacker can deploy a fee-on-transfer or deflationary token and desynchronize the checking account's real per-token balance from the credited `Erc20Credit` amounts recorded in the XCM holding register.

## Finding Description
`withdraw_asset_with_surplus` transfers `amount` to the checking account and, on a truthy boolean return, unconditionally mints `Erc20Credit(amount)` into the XCM holding register regardless of how much the checking account actually received [1](#0-0) . Symmetrically, `deposit_asset_with_surplus` instructs the checking account to send `amount` to the beneficiary and trusts only the boolean success flag, not any measured balance movement [2](#0-1) . The `Erc20Credit` type used for this holding-register accounting is explicitly documented as performing no runtime-level balance enforcement, relying entirely on the ERC20 contract's own behavior [3](#0-2) .

There is no allow-list restricting which ERC20 contracts can be transacted: `ERC20Matcher` matches any `Location` with a local `AccountKey20` junction to an arbitrary `H160` contract address [4](#0-3) , and this is wired directly into `AssetTransactors` on Asset Hub Westend [5](#0-4) . Contract deployment itself is gated only by `EnsureSigned`, with `AllowEVMBytecode` enabled, meaning any unprivileged signed account can deploy an arbitrary ERC20 contract with fee-on-transfer/deflationary `transfer` semantics [6](#0-5) . The PR that introduced this transactor confirms the design intent — any smart contract address matching the `AccountKey20` pattern is treated as a valid ERC20 asset for XCM purposes, with no vetting step [7](#0-6) .

## Impact Explanation
Since the runtime credits the XCM holding register with the pre-fee nominal `amount` while the checking account's real per-token balance can increase/decrease by a different amount, the real ERC20 balance held by `TransfersCheckingAccount` for that token becomes desynchronized from the sum of outstanding `Erc20Credit` entries across in-flight XCM programs. This falls under "theft or unbacked mint or unlock" — deposits drawing on this shared checking account can extract more real tokens than were actually escrowed for a given operation, or conversely cause later legitimate deposits to fail when the contract-enforced transfer reverts due to insufficient real balance, leading to fund lock.

## Likelihood Explanation
No privileged actor is required: `pallet-revive`'s `InstantiateOrigin`/`UploadOrigin` are `EnsureSigned`, so any unprivileged user can deploy a Solidity ERC20 with custom `transfer` semantics, and `PolkadotXcm::execute` is a standard public entrypoint for issuing `withdraw_asset`/`deposit_asset` programs against that contract, as demonstrated by the existing test `withdraw_and_deposit_erc20s` which exercises exactly this code path with a real deployed contract [8](#0-7) . Fee-on-transfer/deflationary token semantics are a well-known, common real-world ERC20 pattern, making this readily reachable and repeatable.

## Recommendation
Measure the checking account's (or beneficiary's) actual token balance before and after the `transferCall` and use that measured delta — not the nominal `amount` — when constructing `Erc20Credit` in `withdraw_asset_with_surplus` and when validating success in `deposit_asset_with_surplus`. Alternatively, restrict `ERC20Matcher` to an allow-list of tokens verified to transfer the exact requested amount with no fee/burn/rebase behavior.

## Proof of Concept
1. As an unprivileged account, deploy an ERC20 contract via `pallet_revive::Pallet::instantiate`/`bare_instantiate` whose `transfer` function burns/deducts a percentage of every transfer.
2. Submit an XCM program via `PolkadotXcm::execute` performing `withdraw_asset` for `amount = 1000` of this token, mirroring the existing `withdraw_and_deposit_erc20s` test structure [9](#0-8) .
3. Observe that `withdraw_asset_with_surplus` credits `Erc20Credit(1000)` into the holding register even though the checking account's real balance only increased by the post-fee amount.
4. Complete the program with `deposit_asset` for the full `1000` credited amount to a beneficiary; `deposit_asset_with_surplus` instructs a transfer of `1000` from the checking account, which either fails (insufficient real balance, causing fund lock) or succeeds by drawing down balance belonging to other outstanding/concurrent `Erc20Credit` entries for that token (unbacked extraction).

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-89)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
impl UnsafeConstructorDestructor<u128> for Erc20Credit {
	fn unsafe_clone(&self) -> Box<dyn ImbalanceAccounting<u128>> {
		Box::new(Erc20Credit(self.0))
	}
	fn forget_imbalance(&mut self) -> u128 {
		let amount = self.0;
		self.0 = 0;
		amount
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-203)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L251-280)
```rust
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L157-161)
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1398-1400)
```rust
	type AllowEVMBytecode = ConstBool<true>;
	type UploadOrigin = EnsureSigned<Self::AccountId>;
	type InstantiateOrigin = EnsureSigned<Self::AccountId>;
```

**File:** prdoc/stable2506/pr_7762.prdoc (L6-19)
```text
doc:
  - audience: Runtime Dev
    description: |
      This PR introduces an Asset Transactor for dealing with ERC20 tokens and adds it to Asset Hub
      Westend.
      This means asset ids of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` will be
      matched by this transactor and the corresponding `transfer` function will be called in the
      smart contract whose address is `key`.
      If your chain uses `pallet-revive`, you can support ERC20s as well by adding the transactor, which lives
      in `assets-common`.
  - audience: Runtime User
    description: |
      This PR allows ERC20 tokens on Asset Hub to be referenced in XCM via their smart contract address.
      This is the first step towards cross-chain transferring ERC20s created on the Hub.
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
