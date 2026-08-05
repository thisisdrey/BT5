This is a critical finding. The `ERC20Matcher` config resolves the asset contract address directly from the XCM asset's `Location` junction — `AccountKey20ToH160::convert` maps `(0, [AccountKey20 { key, .. }])` directly to `H160(key)`, with no allowlist, no registry, and no governance gate [1](#0-0) [2](#0-1) . This means **any unprivileged attacker can pick an arbitrary contract address** by simply crafting an XCM `Asset` with `id = (0, [AccountKey20 { key: <attacker-deployed contract>, network }])` — no admin/governance action is needed to "register" the asset, confirming the report's unresolved likelihood question and making this fully attacker-reachable.

Given that, `withdraw_asset_with_surplus`/`deposit_asset_with_surplus` call `pallet_revive::Pallet::<T>::bare_call` into that attacker-chosen address and trust the ABI-decoded boolean return exactly as described [3](#0-2) [4](#0-3) , with `Erc20Credit` performing no independent balance enforcement [5](#0-4) .

For the `withdraw_asset_with_surplus` path specifically, though, the call origin is `who` (the account attempting the XCM withdrawal, converted via `LocationToAccountId`) [6](#0-5)  — i.e., the "withdraw" is really just `who` calling `transfer` on an arbitrary contract they name themselves and getting an XCM holding credit if the contract reports `true`. Since the attacker fully controls the contract's bytecode (it can be a trivial contract whose `transfer` always returns `true` and does nothing), calling `withdraw_asset_with_surplus` with `asset_id = <attacker's own worthless/no-op contract>` lets the attacker mint an `AssetsInHolding` credit for an arbitrary `amount` with zero real value backing it, which can then be deposited elsewhere (e.g., via `deposit_asset_with_surplus` against the same attacker contract, or teleported/reserve-transferred if the destination chain trusts the asset id blindly). This is a genuine unbacked-mint pattern reachable by any unprivileged XCM-issuing account (e.g., via `pallet_xcm::execute`/`send` with a self-authored program, or as a reserve-asset in a cross-chain transfer), matching the "theft or unbacked mint" impact gate.

Audit Report

## Title
`ERC20Transactor` mints unbacked `AssetsInHolding` credit from a self-reported boolean return of an arbitrary, attacker-chosen ERC20 contract call - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Matcher` derives the target contract address directly and unconditionally from the `AccountKey20` junction of the XCM asset `Location`, with no registry or governance gate, so any unprivileged party constructing an XCM program controls which contract `withdraw_asset_with_surplus`/`deposit_asset_with_surplus` calls. Both functions then trust the callee's self-reported ABI-decoded boolean return from `transfer` as proof that `amount` tokens moved, crediting/debiting `AssetsInHolding` without any independent `balanceOf` delta check, letting an attacker mint XCM holding credit backed by nothing.

## Finding Description
`ERC20Matcher = MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>` matches any local `Location` of the form `(0, [AccountKey20 { key, .. }])` and converts it straight to the contract address `H160(key)`, with `AccountKey20ToH160::convert` performing no allowlisting. `withdraw_asset_with_surplus` takes this attacker-supplied `asset_id`, calls `pallet_revive::Pallet::<T>::bare_call` with the `who` origin encoding a `transfer(checking_address, amount)` payload, and if the call doesn't revert and `abi_decode_returns_validate` yields `true`, unconditionally constructs `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` — there is no pre/post `balanceOf` check on either the caller or the checking account. Because the attacker deploys and fully controls the contract code at `asset_id` (there is no registration/governance step binding asset ids to vetted contracts), they can trivially deploy a contract whose `transfer` function always returns `true` (or reverts selectively) while performing no real balance movement, causing the runtime to credit holding with an arbitrary, unbacked `amount`. The symmetric `deposit_asset_with_surplus` path has the same trust-the-return-value flaw for crediting a beneficiary.

## Impact Explanation
This produces unbacked mint: the XCM executor's `AssetsInHolding` is credited with tokens that were never actually transferred, matching the "theft or unbacked mint" impact category. That holding credit can subsequently be deposited to any beneficiary account via the same transactor's deposit path (again reusing the attacker's own no-op contract, or a different destination), or potentially forwarded in further XCM instructions (e.g., reserve transfer/teleport) depending on downstream trust of the asset id — this could inflate apparent balances of an ERC20 asset id that other parts of the chain (e.g., DEX pools, precompile balance views) might treat as genuine.

## Likelihood Explanation
Exploitability requires only the ability to author/execute an XCM program that references an `AccountKey20` asset id pointing at attacker-deployed contract bytecode, which is achievable by any account able to submit `pallet_xcm::execute` or otherwise trigger the `ERC20Transactor` (part of `AssetTransactors` on Asset Hub Westend) with a self-crafted asset/location — this requires no governance, no relayer compromise, and no privileged role. The `ERC20Matcher`'s direct, unrestricted `Location → H160` mapping (confirmed in `cumulus/parachains/runtimes/assets/common/src/lib.rs`) removes the "governance-restricted mapping" caveat the original report flagged as unresolved, so this is a fully unprivileged, reachable exploit path, not merely a structural weakness.

## Recommendation
Do not rely solely on the decoded boolean return of `transfer`. Read the relevant account's `balanceOf` (checking account for withdraw, beneficiary for deposit) immediately before and after the `bare_call`, and only credit/debit `AssetsInHolding` with the actually observed balance delta, erroring if it does not equal `amount`. Consider also restricting which contract addresses can back an ERC20 asset id (e.g., via a registration/allowlist step) rather than allowing any `AccountKey20` location to resolve directly to arbitrary attacker-controlled bytecode, and add explicit reentrancy protection around the nested `bare_call`.

## Proof of Concept
1. Deploy a minimal ERC20-like contract via `pallet-revive` whose `transfer(address,uint256)` selector always returns `abi.encode(true)` without updating any storage/balance.
2. Submit an XCM program (e.g., via `pallet_xcm::execute`) containing a `WithdrawAsset` instruction for `Asset { id: (0, [AccountKey20 { key: <deployed contract address>, network: None }]).into(), fun: Fungible(amount) }` from the attacker's own signed origin.
3. Observe `ERC20Transactor::withdraw_asset_with_surplus` invoke `bare_call` into the attacker contract, decode `true`, and construct `AssetsInHolding::new_from_fungible_credit` for `amount` with no real ERC20 balance change having occurred.
4. Follow with a `DepositAsset` instruction targeting a chosen beneficiary for the same asset id, confirming `deposit_asset_with_surplus` again calls the attacker contract's `transfer` (which can be made to unconditionally return `true`) and completes without error, `Ok(surplus)`, crediting the beneficiary's apparent balance while no genuine value was moved.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L141-155)
```rust
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L157-160)
```rust
/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```

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
