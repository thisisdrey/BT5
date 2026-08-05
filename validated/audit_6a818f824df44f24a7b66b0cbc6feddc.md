Audit Report

## Title
ERC20 XCM Asset Transactor credits `AssetsInHolding` from an unchecked ERC-20 boolean return instead of a measured balance delta - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` derive XCM `AssetsInHolding` credit purely from the caller-supplied `amount` and a decoded `bool` return of an ERC-20 `transfer()` call, without ever comparing the real on-chain balance of the checking/beneficiary account before and after the call. Because the matched asset contract (`asset_id`/`asset_contract_id`) is any `pallet_revive` contract reachable via an `AccountKey20` location, an attacker who can deploy contracts can make the transactor "believe" a transfer of arbitrary `amount` occurred when none did.

## Finding Description
`withdraw_asset_with_surplus` extracts `(asset_id, amount)` via `Matcher::matches_fungibles(what)` [1](#0-0)  and calls `pallet_revive::Pallet::<T>::bare_call` on `asset_id` with a Solidity `transfer(checking_address, amount)` payload [2](#0-1) . If the call does not revert and ABI-decodes to `true`, it unconditionally constructs `AssetsInHolding::new_from_fungible_credit` with the requested `amount`, with no check of the checking account's actual balance delta [3](#0-2) . `deposit_asset_with_surplus` mirrors this pattern for the beneficiary side [4](#0-3) .

Critically, the `Matcher` wired for this transactor in the codebase, `ERC20Matcher`, matches on `IsLocalAccountKey20`, i.e., *any* location of the shape `(0, [AccountKey20 { .. }])` — there is no allowlist or registry restricting which contract addresses are treated as valid ERC-20 assets [5](#0-4) . The asset identity (`asset_id`) is the raw contract address decoded from the XCM `AccountKey20` junction, meaning the transactor will attempt to `bare_call::transfer` on whatever contract address is supplied in the XCM message. Since contract deployment on `pallet_revive` is generally permissionless for unprivileged signed accounts, an attacker can deploy a contract whose `transfer()` selector always returns `true` while doing no real balance accounting, causing the transactor to mint holding-register credit equal to any `amount` the attacker requests, with zero real value moved.

Existing checks (`return_value.did_revert()`, ABI-decode of the returned bool) only validate that the call didn't revert and returned `true` — they do not validate that value actually moved, which is exactly the missing invariant.

## Impact Explanation
This allows an unprivileged attacker to fabricate `AssetsInHolding` credit backed by nothing, which the XCM executor will then deposit to a beneficiary or forward across a reserve/teleport hop — an unbacked mint via a public XCM entry point, matching the "theft or unbacked mint" / "duplicate settlement" impact categories in the accepted impact gate. The exact corrupted value is the `AssetsInHolding` credit amount constructed in `withdraw_asset_with_surplus` (and the implied "success" in `deposit_asset_with_surplus`), which is not backed by any verified balance change in `TransfersCheckingAccount` or the beneficiary account.

## Likelihood Explanation
High, given: (1) `ERC20Matcher` accepts any `AccountKey20` location with no allowlist [5](#0-4) ; (2) contract deployment via `pallet_revive` is unprivileged for ordinary signed accounts in the general case; (3) the exploit requires only deploying one trivial contract and submitting a single XCM message routed through `withdraw_asset_with_surplus`/`deposit_asset_with_surplus`. No validator, governance, relayer, or key-compromise assumptions are needed — this is a pure code-logic gap in the transactor.

## Recommendation
Measure the checking/beneficiary account's real ERC-20 balance immediately before and after the `bare_call`, and derive the credited `AssetsInHolding` amount (or deposit success) strictly from the observed delta rather than trusting the caller-supplied `amount` combined with a decoded `bool`. Also consider blocking reentrant calls into XCM/precompile execution paths from within the invoked `transfer`.

## Proof of Concept
1. Deploy a minimal `pallet_revive` contract exposing `IERC20::transfer(address,uint256)` that always returns `true` and performs no real balance state changes.
2. Submit an XCM program containing `WithdrawAsset`/`DepositAsset` referencing this contract's address as an `AccountKey20` fungible asset with an arbitrary `amount`.
3. `withdraw_asset_with_surplus` calls the fake `transfer`, decodes `true`, and credits `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` with the full requested `amount` [3](#0-2)  despite `TransfersCheckingAccount` never receiving real value.
4. The XCM executor treats this credit as genuine and deposits it to a beneficiary, completing an unbacked mint with no real transfer occurring.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-159)
```rust
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-181)
```rust
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L195-203)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-279)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L157-160)
```rust
/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```
