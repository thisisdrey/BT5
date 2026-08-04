Confirmed: `ERC20Transactor` is wired into `AssetTransactors` on Asset Hub Westend and used for cross-chain XCM handling of any ERC20 contract address expressed as `AccountKey20` location, backed by `ERC20TransfersCheckingAccount` (a `pallet_revive` bare-call based escrow). [1](#0-0) 

### Title
Fee-on-transfer ERC20 tokens create unbacked XCM holding credit in `ERC20Transactor::withdraw_asset_with_surplus` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` allows any ERC20 contract, referenced via a bare `AccountKey20` XCM location, to be used as an XCM-transactable asset on Asset Hub. When withdrawing such an asset, the transactor calls `transfer(checking_address, amount)` on the arbitrary ERC20 contract and, if the call returns `true`, unconditionally credits the XCM holding register with the full nominal `amount` requested — never checking the checking account's actual balance delta.

### Finding Description
In `withdraw_asset_with_surplus`, the code builds an ERC20 `transferCall` for exactly `amount` and sends it to `ERC20TransfersCheckingAccount`: [2](#0-1) 

It then only checks whether the call reverted and whether the ABI-decoded boolean return is `true`; it never reads the checking account's actual pre/post ERC20 balance to confirm `amount` tokens were truly received: [3](#0-2) 

If `is_success` is `true`, the XCM holding register is credited with the full requested `amount` via `Erc20Credit(amount)`, regardless of what the checking account actually received: [4](#0-3) 

Because `ERC20Transactor` accepts *any* smart-contract address expressible as an `AccountKey20` location as a valid XCM asset (there is no registry or whitelist gating which contracts can be used — matching is done generically by `assets_common::ERC20Matcher` against the location), a user can pick a fee-on-transfer/deflationary ERC20 contract (e.g. one implementing a burn-on-transfer or reflection fee, as in `STA`, `PAXG`-style tokens or a custom malicious contract deployed via `pallet-revive` itself) as the withdrawn asset. `transfer()` on such a token returns `true` while delivering fewer tokens than `amount` to the checking account, but the XCM engine still treats `amount` as backed inside `AssetsInHolding`.

This corrupted holding value (`Erc20Credit(amount)`, larger than the checking account's real balance increase) then flows into subsequent XCM instructions in the same program — e.g. `DepositAsset`/`InitiateTransfer` to another chain, or reserve-style forwarding through the bridge/HRMP path — which mint or forward the inflated nominal amount downstream, while the checking-account "reserve" backing it on Asset Hub is short by the fee amount. This is the direct analog of the "fee-on-transfer" bug class: the code assumes `balance_before - balance_after == amount` instead of verifying it.

### Impact Explanation
This breaks the "conserve value / settle exactly once for the rightful amount" invariant for asset transactors: the XCM holding register (and anything built downstream from it, such as a `DepositAsset` to a beneficiary or a further reserve-transfer to another chain/bridge) is inflated relative to the actual ERC20 tokens escrowed in `ERC20TransfersCheckingAccount`. An attacker can repeatedly withdraw fee-on-transfer tokens and deposit the inflated nominal amount to any beneficiary (including forwarding cross-chain), generating unbacked value out of thin air relative to the real token reserve, which is a theft/unbacked-mint class impact.

### Likelihood Explanation
This requires only an unprivileged user with a signed origin to submit an XCM program (e.g. via `pallet_xcm::execute`/`transfer_assets`) referencing a self-deployed or existing fee-on-transfer ERC20 contract as the asset — no privileged/governance action, malicious relayer, or validator collusion is needed. `pallet-revive` contract deployment is itself permissionless, so an attacker can trivially deploy a minimal ERC20 with a `transfer` function that always returns `true` while moving less than `value` tokens.

### Recommendation
In `withdraw_asset_with_surplus`, read the checking account's ERC20 `balanceOf` before and after the `transferCall`, and use `after - before` (not the requested `amount`) as the value credited into `AssetsInHolding`/`Erc20Credit`. Apply the same before/after balance check symmetrically in `deposit_asset_with_surplus` on the beneficiary side. Consider additionally maintaining a governance-controlled allow-list of ERC20 contracts eligible for `ERC20Transactor` to reduce exposure to adversarial token contracts entirely.

### Proof of Concept
1. Deploy (via `pallet_revive`, permissionlessly) a minimal ERC20 contract `EvilFeeToken` whose `transfer(to, value)` moves `value * 90 / 100` to `to`, burns the remaining 10%, and returns `true` unconditionally.
2. Mint a large `EvilFeeToken` balance to attacker account `A` on Asset Hub.
3. Submit an XCM program from `A`: `WithdrawAsset([AccountKey20{EvilFeeToken}, amount: 1000])`, `DepositAsset(beneficiary: A2)` (or forward via `InitiateTransfer` to another chain/bridge).
4. Observe: `ERC20Transactor::withdraw_asset_with_surplus` credits holding with `1000`, but `ERC20TransfersCheckingAccount`'s real `EvilFeeToken` balance only increased by `900`.
5. The subsequent `DepositAsset`/cross-chain forward moves/mints the full `1000` nominal amount to `A2` (or the remote chain), while only `900` tokens are actually escrowed — a 100-unit unbacked value has been created, repeatable indefinitely.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-246)
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

/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
);
```

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
