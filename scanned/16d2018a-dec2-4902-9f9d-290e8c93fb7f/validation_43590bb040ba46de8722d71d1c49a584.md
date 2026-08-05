## Analysis

The ERC777 report's core broken invariant is: **the protocol determines "how much was received" by trusting a single external call's outcome (a balance snapshot straddling an attacker-controlled hook) instead of an atomic, hook-free measurement, letting the attacker manufacture credit that was never actually backed by a real transfer.**

The strongest local analog is Asset Hub's new **ERC20 XCM Asset Transactor**, which bridges XCM's asset-holding model to arbitrary `pallet-revive` smart contracts.### Title
ERC20 XCM Asset Transactor credits `AssetsInHolding` from an unchecked ERC-20 boolean return instead of a measured balance delta - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
The Polygon report's root cause is that the bridge derives "amount received" from a call that can be manipulated by the token contract itself (via a re-entrant hook), rather than from an atomically-verified transfer outcome. The local analog is `ERC20Transactor::withdraw_asset_with_surplus` / `deposit_asset_with_surplus`, which mint XCM holding-register credit (`AssetsInHolding`) equal to the *requested* `amount` based solely on decoding a `bool` return value from a `pallet_revive::Pallet::<T>::bare_call` to an arbitrary user-deployed ERC20 contract's `transfer()` function. There is no post-call balance check of the checking/beneficiary account, so the actual value moved by the (attacker-controlled) contract's bytecode can diverge arbitrarily from `amount` while still satisfying the transactor.

### Finding Description
`withdraw_asset_with_surplus` (`erc20_transactor.rs:150-216`) does:
1. `Matcher::matches_fungibles(what)` extracts `(asset_id, amount)` from the XCM `Asset` being withdrawn. [1](#0-0) 
2. It calls `pallet_revive::Pallet::<T>::bare_call` on `asset_id` (an arbitrary smart-contract address) with the Solidity `transfer(checking_address, amount)` selector. [2](#0-1) 
3. If the call does not revert and ABI-decodes to `true`, it unconditionally constructs `AssetsInHolding` credited with the *requested* `amount` — not any measured change in the checking account's real balance. [3](#0-2) 

`deposit_asset_with_surplus` mirrors this: it calls `transfer(beneficiary, amount)` from the checking account and, on decoding `true`, simply reports success/`surplus` weight — again with no verification that the beneficiary's balance actually increased by `amount`. [4](#0-3) 

Because `asset_id` is any contract address matched via `Matcher::matches_fungibles` (an `AccountKey20` XCM location), and contract deployment on Asset Hub is permissionless (`UploadOrigin: EnsureSigned`), an unprivileged attacker can deploy a trivial contract whose `transfer()` selector always returns `true` while performing no real balance movement, or one that reenters other pallets/precompiles during the call (via `XcmPrecompile<Self>` or `AssetConversion` precompile also wired into the same `pallet_revive::Config::Precompiles` tuple). The transactor has no equivalent of the Polygon fix's `balanceBefore/balanceAfter` diff, so it cannot detect that the ERC20 contract's `transfer` did not actually move value, or that it re-entered to manipulate state mid-call. The XCM executor then treats the resulting `AssetsInHolding` as genuine backed value and proceeds to deposit it to any beneficiary, potentially across a reserve/teleport hop where a *different*,真实ly-backed representation is minted or released based on this unverified credit.

Existing reentrancy guards in `pallet-revive` (`ReentranceDenied`, `allows_reentry`) only prevent the *callee* from calling back into the *same caller contract's* frame; they do not, and cannot, prevent the callee contract from lying about its own balance changes to the transactor, since the transactor never reads the balance at all.

### Impact Explanation
This lets an unprivileged attacker fabricate XCM holding-register credit for a "reserve-backed" ERC20 asset with no real value transferred, then have that credit deposited to a beneficiary — i.e., unbacked mint / asset-value non-conservation via a public entry point (any XCM message routed through this transactor, e.g., `pallet_xcm::execute`/`send`, reserve transfers, or Transact instructions targeting this runtime). If the checking account is used as the reserve backing for a cross-chain representation, this can drain or desynchronize reserve backing, producing duplicate/unbacked settlement — squarely in the "theft or unbacked mint" and "duplicate settlement" impact categories.

### Likelihood Explanation
High for any deployment that wires `ERC20Transactor` to accept attacker-deployable contracts as `Matcher`-recognized fungible assets (contract deployment via pallet-revive is permissionless under `EnsureSigned`). The attack requires only: (1) deploying a minimal contract with a `transfer()` function returning `true` without honoring real balances, and (2) issuing/relaying one XCM message that withdraws that "asset." No validator, relayer, governance, or leaked-key assumptions are needed.

### Recommendation
Measure the checking/beneficiary account's real ERC20 balance immediately before and after the `bare_call`, and credit `AssetsInHolding` (or treat the deposit as successful) only for the actual observed delta — mirroring the Polygon bridge's post-incident fix — rather than trusting the boolean return value and the caller-supplied `amount`. Additionally, consider disallowing reentrant precompile/XCM calls from within the ERC20 `transfer` execution triggered by this transactor.

### Proof of Concept
1. Attacker deploys, via `pallet_revive::Pallet::<T>::instantiate`/`upload_code` (permissionless, `EnsureSigned`), a minimal contract exposing the `IERC20::transfer(address,uint256)` selector that always returns `true` and performs no real storage/balance accounting (or optionally reenters the `XcmPrecompile`/`AssetConversion` precompile mid-call).
2. Attacker registers this contract's address as an `AccountKey20` location matched by the runtime's `Matcher::matches_fungibles` for the `ERC20Transactor` (or targets a runtime where this is already wired for a "supported" ERC20 class).
3. Attacker submits an XCM program (`WithdrawAsset` + `DepositAsset`/`InitiateReserveWithdraw`) referencing this asset with an arbitrary `amount`.
4. `withdraw_asset_with_surplus` calls the attacker contract's `transfer`, receives `true`, and credits `AssetsInHolding` with the full requested `amount` — no real value ever moved to `TransfersCheckingAccount`. [3](#0-2) 
5. The XCM executor deposits this fabricated holding to a beneficiary (or forwards it as a reserve-backed asset to another chain), completing an unbacked "mint"/settlement with no corresponding real transfer.

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
