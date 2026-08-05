Confirmed: `ERC20Matcher` matches **any** location of the form `(0, [AccountKey20{key,..}])`, i.e. any contract address deployed via `pallet-revive` on Asset Hub, with no whitelist or allow-list check [1](#0-0) , and this matcher is wired directly into the live `AssetTransactors` list of Asset Hub Westend's XCM config [2](#0-1) .

### Title
Unbacked value creation in `ERC20Transactor::withdraw_asset_with_surplus` via ERC20 tokens with non-standard transfer semantics (fee-on-transfer/rebasing/deflationary) - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor`, the `TransactAsset` implementation used to let XCM operate on arbitrary ERC20 contracts deployed via `pallet-revive` on Asset Hub, credits the XCM `AssetsInHolding` register with the *requested* `amount` whenever the underlying `transferCall` returns `true`, rather than the *actual* balance change observed at the checking account. Any unprivileged user can deploy an ERC20-compatible contract with non-standard transfer semantics (fee-on-transfer, rebasing, deflationary, or any contract that returns `true` while moving less/more than `value`) and use it through this transactor.

### Finding Description
`withdraw_asset_with_surplus` calls the ERC20 contract's `transfer(checking_address, amount)` and, if the ABI-decoded return value is `true` (regardless of actual token movement), constructs `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` — an imbalance object whose `amount()` is hardcoded to the requested `amount`, not to any measured delta of the checking account's real ERC20 balance [3](#0-2) . There is no `balanceOf` check before/after the call to validate that the checking account actually received `amount` tokens.

Symmetrically, `deposit_asset_with_surplus` calls `transfer(beneficiary, amount)` from the checking account and treats a `true` return as fully successful regardless of whether the beneficiary actually received `amount` [4](#0-3) .

Because `ERC20Matcher` accepts any `AccountKey20` location without any allow-list, and `pallet-revive` allows anyone to permissionlessly deploy arbitrary contracts [5](#0-4) , an attacker can:
1. Deploy a malicious/fee-on-transfer ERC20 whose `transfer()` moves less than `value` to the checking account but still returns `true`.
2. Submit an XCM program (e.g. via `pallet_xcm::execute`/`send` or any XCM-triggered transfer) that withdraws `amount` of that token from the attacker's own balance.
3. The executor's holding register is credited with the full `amount`, an amount the checking account (the pallet's reserve/backing account) never actually received.
4. That inflated holding can subsequently be `DepositAsset`-ed to any beneficiary — including forwarding cross-chain via reserve-transfer — creating value that is not backed by any real ERC20 balance held by `ERC20TransfersCheckingAccount`.

This directly mirrors the external report's core broken invariant: the protocol assumes ERC20 `transfer()` return value and requested `amount` are a reliable proxy for actual balance movement, which is false for fee-on-transfer, rebasing, or deliberately malicious tokens, and the design permits any ERC20 contract without a whitelist.

### Impact Explanation
This is theft/unbacked-value creation reachable from an unprivileged public entrypoint (XCM execution over any user-deployed `pallet-revive` contract) — no malicious validator, relayer, or governance actor is needed. Repeated exploitation can drain the real token balance held by the checking account (fund loss for legitimate ERC20 holders using the transactor) while the attacker's XCM holding register reflects a phantom, uncollateralized balance that can be moved to other accounts or even bridged off-chain, amplifying the loss.

### Likelihood Explanation
Likelihood is high for chains that enable `pallet-revive` and wire in `ERC20Transactor` (as Asset Hub Westend already does) since: (a) contract deployment is permissionless, (b) `ERC20Matcher` performs no allow-listing, and (c) crafting a `transfer()` that returns `true` while shorting the actual amount (fee-on-transfer / deflationary pattern) is trivial Solidity and is a well-known real-world token pattern (e.g. USDT-style deflationary tokens, many rebasing tokens).

### Recommendation
- After calling `transfer`, measure the actual `balanceOf` delta of the checking account (or beneficiary) and credit/consume exactly that delta instead of the requested `amount`.
- Alternatively/additionally, maintain a governance-controlled allow-list of ERC20 contract addresses permitted to be used with `ERC20Transactor`, rejecting any contract not vetted for standard, non-rebasing, non-fee-on-transfer behavior — matching the client's stated mitigation approach in the external report.
- Add fuzz/integration tests using a fee-on-transfer and a rebasing mock ERC20 to confirm the transactor cannot create value.

### Proof of Concept
1. Deploy `EvilERC20` via `pallet_revive::instantiate` whose `transfer(to, value)` internally moves only `value - fee` to `to`, credits the sender's balance decreased by `value`, but still returns `true`.
2. Fund attacker's Asset Hub account with `X` units of `EvilERC20` and grant the account enough gas/deposit to invoke XCM.
3. Attacker submits `pallet_xcm::execute` with an XCM program: `WithdrawAsset(Asset{ id: EvilERC20 location, fun: Fungible(X) })`, `DepositAsset { assets: All, beneficiary: attacker_or_other }`.
4. In `withdraw_asset_with_surplus`, `transfer(checking_account, X)` executes and returns `true`, but checking account's real ERC20 balance only increases by `X - fee`.
5. `AssetsInHolding` is nonetheless credited with `X` [6](#0-5) ; `deposit_asset_with_surplus` then transfers `X` back out of the checking account to the beneficiary, which now holds more real value in aggregate than the checking account can back for other legitimate holders of the same token, demonstrating unbacked/duplicate value creation.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-160)
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
