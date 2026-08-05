Confirmed: `ERC20Matcher` (`cumulus/parachains/runtimes/assets/common/src/lib.rs:159-160`) matches **any** local `AccountKey20` location, i.e., any contract address on the chain, with no allowlist. Combined with `ERC20Transactor`'s trust in the raw `bool` return value of `transferCall`, this reproduces the report's core defect ("assume full amount transferred") in a form that lets an unprivileged attacker mint unbacked XCM value.

### Title
ERC20 asset transactor credits full requested amount into XCM holding based only on a contract's boolean return, not actual balance change - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` calls an ERC20 contract's `transfer(checking_account, amount)` and, if the ABI-decoded return value is `true`, unconditionally credits the full `amount` into the XCM `AssetsInHolding` register via `Erc20Credit(amount)`. It never checks the actual balance delta of the checking account. `deposit_asset_with_surplus` behaves symmetrically. Because `ERC20Matcher` accepts any local `AccountKey20` contract as a valid fungible asset with no allowlist [1](#0-0) , any user can deploy a contract whose `transfer()` returns `true` while moving little or no real value, and use it to inflate XCM holding credits.

### Finding Description
`withdraw_asset_with_surplus` performs the withdrawal purely by calling the token's `transfer` function and trusting its decoded boolean result: [2](#0-1) 

There is no `balanceOf` check before/after the call, unlike the fix Beanstalk applied for the reported bug (comparing `balanceOf` deltas rather than trusting the nominal `amount`). The credited `Erc20Credit(amount)` object is a pure accounting abstraction with "no real backing" by design, per its own doc comment: [3](#0-2) 

`deposit_asset_with_surplus` similarly just calls `transfer` to the beneficiary and returns success based on the decoded boolean, again without verifying actual balance movement: [4](#0-3) 

The matcher that decides which locations are treated as valid ERC20 assets accepts *any* local `AccountKey20` address, i.e. any contract deployed by any user, with no registration or governance step: [5](#0-4) 

Putting these together: an attacker deploys a trivial malicious "ERC20" contract on pallet-revive whose `transfer(address,uint256)` function always returns `true` without actually moving `value` tokens (or moves an arbitrary smaller/larger amount than `value`, i.e., a fee-on-transfer / no-op token analogous to the reported bug). The attacker then submits an XCM program (via `pallet_xcm::execute`, permissionless for a signed origin) that:
1. `WithdrawAsset` of `amount` X of the malicious token from the attacker's own account — `ERC20Transactor` calls `transfer(checking_account, X)`, the malicious contract returns `true` while transferring 0 real tokens, and the transactor credits `Erc20Credit(X)` into the XCM holding register.
2. `DepositAsset` of the "held" X to any beneficiary (or reserve-transfer cross-chain) — the holding register genuinely contains `X` worth of `Erc20Credit`, entirely decoupled from any real token balance in the checking account.

Because the register's balance is not tied to real economic value, this credit can be forwarded through further XCM instructions (e.g. `InitiateTransfer`/`DepositReserveAsset` to another chain, or paired with `pay_fees`) to move "value" the attacker never actually possessed, exactly mirroring the underlying invariant break in the Beanstalk report: code assumes `amount == received amount` from an ERC20 `transfer`, but a malicious/nonstandard token can violate that assumption, and here that broken assumption feeds directly into XCM's internal fungible accounting rather than an isolated LP/vault balance.

### Impact Explanation
This breaks the invariant that XCM `AssetsInHolding` credits must be backed 1:1 by real, verified token custody. It allows unbacked "mint" of ERC20-asset value inside the XCM executor's holding register, which can subsequently be moved, deposited to an arbitrary beneficiary, or bridged, unlocking or crediting more value than the checking account actually escrowed. This falls under "theft or unbacked mint" and "public underpriced work" impact categories for the program.

### Likelihood Explanation
The path requires only a normal signed account able to deploy a contract via `pallet-revive` and call `pallet_xcm::execute` — both permissionless operations available today on any chain (e.g. Asset Hub Westend) configuring `ERC20Transactor` in its `AssetTransactors` tuple [6](#0-5) . No governance, admin, relayer, or validator collusion is needed, matching the "unprivileged attacker" requirement.

### Recommendation
Mirror the Beanstalk fix: after calling `transfer`, verify the actual balance delta of the checking account (`balanceOf(checking_account)` before/after) rather than trusting the decoded boolean return value and the nominal `amount`, and credit/settle only the observed delta into `AssetsInHolding`. Apply the same balance-delta verification symmetrically in `deposit_asset_with_surplus`.

### Proof of Concept
1. On a chain with `ERC20Transactor` configured (e.g. asset-hub-westend), deploy a minimal contract implementing `IERC20` where `transfer(address to, uint256 value)` always returns `true` and does not modify any balance (or burns `value` from a non-existent balance / mints nothing).
2. Fund the attacker's revive/H160 account with 0 real balance of this token (no `_mint` needed since `transfer` never checks balances).
3. Submit `pallet_xcm::execute` with:
   - `WithdrawAsset((AccountKey20{key: malicious_contract}, X))`
   - `DepositAsset(All, attacker_beneficiary_or_other_chain)`
4. Observe `ERC20Transactor::withdraw_asset_with_surplus` decode `is_success == true` and credit `Erc20Credit(X)` into holding (`erc20_transactor.rs:195-203`), then `deposit_asset_with_surplus` transfer that credited `X` to the beneficiary, with no real token ever having moved corresponding to `X`, confirming unbacked issuance of accounted value.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L251-279)
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
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L239-246)
```rust
/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
);
```
