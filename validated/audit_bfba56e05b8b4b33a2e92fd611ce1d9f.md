### Title
XCM `ERC20Transactor` trusts the ERC-20 `transfer()` boolean return value instead of verifying actual balance movement, allowing minting of unbacked XCM asset holdings - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor` bridges XCM `Asset`/`Location` semantics to ERC-20 contracts deployed on `pallet-revive`, and is wired into `asset-hub-westend`'s XCM configuration. Both its `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` implementations invoke the token's `transfer()` function via `pallet_revive::Pallet::<T>::bare_call` and treat the call as fully successful purely based on the ABI-decoded boolean return value (`is_success`/`Ok(true)`), exactly the pattern flagged in the Ajna flashloan report: relying solely on a reported "successful transfer" rather than confirming the actual, resulting balance delta. [1](#0-0) [2](#0-1) 

### Finding Description
The Ajna bug's core invariant break is: "a token that reports success on `safeTransfer` without an equivalent, real accounting change lets the protocol believe more value moved than actually did," and the fix demanded a balance-before/after check bracketing the externally-controlled call.

`ERC20Transactor::withdraw_asset_with_surplus` performs the equivalent structure for XCM-managed ERC-20 tokens on Asset Hub:
1. It calls `IERC20::transferCall` on an arbitrary `asset_id` contract (any token registered via the `Matcher`), moving `amount` from `who` to the checking account. [3](#0-2) 
2. It never reads the checking account's or `who`'s actual on-chain ERC-20 balance before/after the call — it only decodes the ABI return value of `transfer()`.
3. If the decoded return is `true`, it unconditionally mints an `AssetsInHolding` credit for the full requested `amount` via `Erc20Credit(amount)`, regardless of whether the checking account balance actually increased by `amount`. [4](#0-3) 

The comment on `Erc20Credit` even states explicitly that "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime" — i.e., the runtime deliberately does not perform an independent invariant check, mirroring precisely the missing-balance-check flaw described in the external report. [5](#0-4) 

Symmetrically, `deposit_asset_with_surplus` calls `transfer()` from the checking account to the beneficiary and treats `Ok(true)` as full, final settlement of the deposit without verifying the beneficiary's real balance changed by `amount`. [6](#0-5) 

Because any contract deployed under `pallet-revive` and registered through the `Matcher` (`MatchesFungibles<H160, u128>`) as an XCM-recognized fungible asset can implement arbitrary `transfer()` logic — e.g., returning `true` while internally withholding, freezing, partially executing, or (in an upgradeable-proxy scenario) changing its accounting semantics after being onboarded — the XCM executor's `AssetsInHolding` state can become inflated relative to the token contract's real balances. This holding can then be moved through the rest of the XCM pipeline (deposited to a beneficiary, reserve-transferred, or held across a barrier) as if it were fully backed, creating value out of nothing from the runtime's perspective. This directly matches the "Pivot" requirement that "Balances, assets, NFTs ... must conserve value and settle exactly once to the rightful beneficiary and amount," and that message/asset state should only advance after execution and settlement genuinely succeed — here it advances merely on a self-reported boolean.

### Impact Explanation
An attacker who can register or interact with an ERC-20 contract that Asset Hub's `Matcher` accepts as an XCM asset (any contract deployed via `pallet-revive`, permissionless) can cause `withdraw_asset_with_surplus` to mint `AssetsInHolding` credit that is not backed by an actual increase of the checking account's real ERC-20 balance. This holding is fungible within the XCM executor and can be deposited to any beneficiary, effectively minting unbacked value recognized by the chain's XCM/asset accounting — a direct "theft or unbacked mint" impact, which is explicitly in the accepted impact category for this program.

### Likelihood Explanation
The path requires only an unprivileged actor who can deploy or otherwise interact with a `pallet-revive` contract used as the ERC-20 backing for an XCM asset, and craft (or already possess) a token contract whose `transfer()` returns `true` without moving the full balance (e.g. via internal blacklisting, partial-transfer semantics, or fee/allowance games not anticipated by the naive success check). No malicious relayer, validator, collator, or governance action is needed — the flaw sits purely in the runtime's XCM asset-transacting logic that trusts a contract-controlled boolean.

### Recommendation
Record the checking account's (and, for deposits, the beneficiary's) actual ERC-20 balance via a `balanceOf` call immediately before and after the `transfer()` `bare_call`, and only construct/settle the `AssetsInHolding` credit (or report success) when the measured balance delta equals `amount`. This mirrors the Ajna fix: bracket the externally-controlled call with an authoritative balance check rather than trusting the callee's self-reported success return value.

### Proof of Concept
1. Deploy a Solidity contract `EvilToken` on `pallet-revive` implementing ERC-20 `transfer(address,uint256)` that always returns `true` but internally increments the recipient's balance by less than `value` (or not at all) for certain callers/situations (e.g., blacklists the checking account after N calls, or silently caps transfers).
2. Register `EvilToken`'s address as a valid XCM asset via the runtime's `Matcher` (`MatchesFungibles<H160, u128>`), as is standard for onboarding ERC-20-backed XCM assets on Asset Hub.
3. Submit an XCM program that performs `WithdrawAsset` for `EvilToken` from `who`'s location for `amount`. `ERC20Transactor::withdraw_asset_with_surplus` calls `EvilToken.transfer(checking_address, amount)`, which returns `true` per the return check at [7](#0-6)  even though the checking account's real balance increased by less than `amount`.
4. The executor unconditionally mints `AssetsInHolding` credit of the full `amount` [8](#0-7) , which the attacker then routes via `DepositAsset`/`InitiateTransfer` to any beneficiary or onward chain, realizing value not actually backed by the token contract's ledger.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L163-181)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-297)
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
```
