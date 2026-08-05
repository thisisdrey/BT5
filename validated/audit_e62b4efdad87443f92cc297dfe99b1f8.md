This confirms the claim. The `ERC20Matcher` type at `cumulus/parachains/runtimes/assets/common/src/lib.rs:159-160` matches any local `AccountKey20` location generically via `IsLocalAccountKey20` and `AccountKey20ToH160`, with no allow-list or registry gating which contract addresses are eligible — any deployed contract address can be used. <cite repo="Loderfordw/polkadot-sdk--019" path="cumulus/parachains/runtimes/assets/common/src/lib.rs" start="132="139" /> [1](#0-0) 

This confirms the core premise of the claim: `ERC20Transactor::withdraw_asset_with_surplus` calls `transfer(checking_address, amount)` and, checking only the boolean return value and revert status, unconditionally credits `AssetsInHolding` with the full nominal `amount` via `Erc20Credit(amount)` — never verifying the checking account's actual balance delta. [2](#0-1) [3](#0-2) 

This is wired into `AssetTransactors` on Asset Hub Westend as claimed. [4](#0-3) 

Symmetrically, `deposit_asset_with_surplus` has the identical pattern: it transfers `amount` from the checking account to the beneficiary and treats a `true` return as full success without checking actual balance received by the beneficiary. [5](#0-4) 

All elements of the claim are verified against the actual code: the generic, unrestricted matching of any `AccountKey20` contract, the reliance solely on the boolean return value from `transfer()`, and the unconditional crediting of the nominal `amount` into XCM holding. This is a genuine "assumed conservation" bug affecting an unprivileged, permissionless code path (arbitrary XCM program submission plus permissionless `pallet-revive` contract deployment), matching the required impact category of theft/unbacked mint via corrupted holding-register value.

Audit Report

## Title
Fee-on-transfer ERC20 tokens create unbacked XCM holding credit in `ERC20Transactor::withdraw_asset_with_surplus` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor` allows any smart contract address expressible as a local `AccountKey20` XCM location to be treated as a valid XCM-transactable asset, with no allow-list gating which contracts qualify. In `withdraw_asset_with_surplus`, the transactor calls the ERC20 `transfer(checking_address, amount)` function on the arbitrary contract, and if the call doesn't revert and decodes to `true`, unconditionally credits the XCM holding register with the full nominal `amount` requested, without ever verifying that the checking account's real balance increased by that amount.

## Finding Description
`ERC20Matcher` (`cumulus/parachains/runtimes/assets/common/src/lib.rs:159-160`) matches fungible assets purely based on `IsLocalAccountKey20`/`AccountKey20ToH160`, i.e., any 20-byte contract address encoded as `AccountKey20` in a local location — there is no registry or whitelist restricting eligible contracts.

In `withdraw_asset_with_surplus` (`cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs:159-169`), the code builds a `transferCall` for the full requested `amount` and dispatches it via `pallet_revive::Pallet::<T>::bare_call` against the arbitrary matched contract address (`asset_id`). The only checks performed on the result are whether the call reverted and whether the ABI-decoded return value is boolean `true` (`erc20_transactor.rs:185-207`). If `is_success` is `true`, the code credits `AssetsInHolding` with `Erc20Credit(amount)` — the originally requested nominal amount — regardless of how many tokens the checking account actually received.

Because ERC20 `transfer()` semantics are entirely up to the called contract's bytecode, a fee-on-transfer, deflationary, or otherwise adversarial ERC20 contract can return `true` while delivering fewer tokens than `amount` to `ERC20TransfersCheckingAccount`. Since `pallet-revive` contract deployment is permissionless, an attacker can deploy such a contract and reference it via its `AccountKey20` location in an XCM program. The resulting `AssetsInHolding` credit is inflated relative to the real ERC20 balance backing it in the checking account, and this inflated value flows into subsequent XCM instructions (e.g., `DepositAsset`, reserve-transfer forwarding) in the same program.

Existing checks (revert status and boolean return value) are insufficient because they validate only the ERC20 contract's self-reported success signal, not the actual economic effect of the transfer.

## Impact Explanation
This breaks the value-conservation invariant for the `ERC20Transactor`: the amount credited into XCM holding (and subsequently deposited to a beneficiary or forwarded cross-chain) can exceed the real ERC20 balance change in `ERC20TransfersCheckingAccount`. Repeated exploitation lets an attacker mint unbacked nominal value relative to the real token reserve backing it — an unbacked-mint/theft-class impact under the accepted impact gate ("theft or unbacked mint or unlock").

## Likelihood Explanation
Exploitation requires only an unprivileged signed origin submitting an XCM program (e.g., via `pallet_xcm::transfer_assets`/`execute`) that references a self-deployed fee-on-transfer ERC20 contract. Contract deployment via `pallet-revive` is permissionless, so no governance, validator, or relayer collusion is required, and the attack is trivially repeatable.

## Recommendation
In `withdraw_asset_with_surplus`, read the checking account's ERC20 `balanceOf` before and after the `transferCall`, and credit `AssetsInHolding` with `after - before` rather than the requested `amount`. Apply the symmetric check in `deposit_asset_with_surplus` for the beneficiary's balance. Additionally, consider a governance-controlled allow-list restricting which ERC20 contracts are eligible for `ERC20Transactor` handling.

## Proof of Concept
1. Deploy a minimal ERC20 contract via `pallet_revive` whose `transfer(to, value)` moves `value * 90 / 100` to `to`, discarding the remainder, but always returns `true`.
2. Mint a balance of this token to attacker account `A`.
3. Submit an XCM program from `A`: `WithdrawAsset([AccountKey20{contract}, amount: 1000])` followed by `DepositAsset(beneficiary: A2)`.
4. Observe `withdraw_asset_with_surplus` credits `AssetsInHolding` with `1000` (`Erc20Credit(1000)`), while `ERC20TransfersCheckingAccount`'s real token balance increased by only `900`.
5. `deposit_asset_with_surplus` transfers the nominal `1000` (or whatever the checking account's contract call reports as `true`) to `A2`, while the checking account is short by `100` units — repeatable to drain/inflate value arbitrarily.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L159-160)
```rust
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-280)
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
