Confirmed: `ERC20Matcher` accepts **any** location that ends in an `AccountKey20` junction and converts it directly to an `H160` with no registry or allow-list check. [1](#0-0) 

This `H160` is fed straight into `ERC20Transactor::deposit_asset_with_surplus`, which then executes a `transferCall` **from the privileged `TransfersCheckingAccount` to the XCM message's beneficiary**, using the amount claimed in the XCM `Asset` — an amount that is entirely attacker-supplied and never checked against any actual prior deposit. [2](#0-1) 

### Title
Unvalidated ERC20 contract matching lets attacker drain `TransfersCheckingAccount`'s real token balances via forged XCM asset locations - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, `cumulus/parachains/runtimes/assets/common/src/lib.rs`)

### Summary
`ERC20Matcher` derives the ERC20 contract address (`H160`) directly from the raw `AccountKey20` junction of an incoming XCM `Asset`, with no check that the address corresponds to a token that was actually deposited/reserved for that XCM message. `ERC20Transactor::deposit_asset_with_surplus` then calls `IERC20::transfer` on that attacker-chosen contract **as the privileged `TransfersCheckingAccount`**, moving whatever amount the attacker specified in the `Asset` to a beneficiary they control. This mirrors the reported `LPToken.sol`/`CreditVault.sol` pattern: any "instance" (here, any `H160`) is treated as legitimate without a factory/registry to bind it to a real, previously-escrowed deposit, so `transferOut`-equivalent logic (the `bare_call` to `transfer`) can be triggered against unrelated, real token balances held by the checking account.

### Finding Description
- `ERC20Matcher = MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>` accepts any `Location` of the form `(0, [AccountKey20 { key, .. }])` and blindly converts `key` to `H160`. [3](#0-2) 
- There is no check anywhere in `matches_fungibles`/`AccountKey20ToH160::convert` that the resulting `H160` is a token that Asset Hub actually recognizes as legitimately backing a prior teleport/reserve-transfer receipt, nor that the checking account's balance of that specific contract corresponds to the value asserted in the XCM message.
- `deposit_asset_with_surplus` takes the matched `(asset_contract_id, amount)` pair directly from the XCM `Asset` in holding and issues `bare_call` as `TransfersCheckingAccount` invoking `transfer(beneficiary, amount)` on `asset_contract_id`. [4](#0-3) 
- The `amount` and `asset_contract_id` both originate from attacker-controlled XCM instruction fields (e.g. a `WithdrawAsset`/`ReserveAssetDeposited` in a `Transact`/local `execute` XCM authored by the attacker's own account, or an XCM constructed to be executed via `PolkadotXcm::execute`/teleport paths that route through this transactor). Because the contract identity is never bound to a specific escrow/deposit record (no factory-style registry as recommended in the original report), an attacker can name any `H160` — including a real, valuable ERC20 that `TransfersCheckingAccount` legitimately holds a balance of from unrelated prior operations — and specify an arbitrary `amount` up to that balance.
- This directly parallels the reported bug class: `supportMarket()`/`transferOut()` trusted an unauthenticated `LPToken.sol` address; here `matches_fungibles`/`deposit_asset_with_surplus` trusts an unauthenticated `H160` address and moves value out of a shared privileged account without verifying provenance of the deposit being claimed.

### Impact Explanation
`TransfersCheckingAccount` is a shared, privileged EVM-mapped account used across ERC20-backed XCM asset flows on the parachain. If an attacker can invoke this transactor path with a forged `Asset` (arbitrary `H160` + arbitrary `amount`), they cause the runtime to execute a real `transfer` call moving genuine ERC20 balance held by the checking account to an address of their choosing — theft of funds that were never actually deposited by the attacker, i.e., unbacked withdrawal / duplicate settlement of value that was never escrowed for this XCM. This matches "theft or unbacked mint or unlock" and "runtime bugs that compromise intended behavior" in the impact gate.

### Likelihood Explanation
This does not require a malicious validator, collator, relayer, or governance/admin action — an ordinary user (or any XCM sender able to reach the executor, e.g. via `pallet_xcm::execute` or a reserve-transfer that gets routed through this transactor in the asset-hub runtime's `AssetTransactors`) can encode an `Asset` with an arbitrary `AccountKey20` and amount. The exploit is entirely self-contained to a single crafted XCM program plus knowledge of a real ERC20 contract address the checking account holds balance in — no privileged or off-chain cooperation needed. The main uncertainty is the exact `AssetTransactors` wiring in the live runtime config (`asset-hub-westend/src/xcm_config.rs`) that determines which XCM instructions reach `ERC20Transactor` and under what origin filtering, which limits my full certainty and should be verified directly in that file.

### Recommendation
Bind the ERC20 contract identity accepted by `ERC20Matcher`/`ERC20Transactor` to a governance-maintained registry (factory-style allow-list) of legitimate ERC20 contracts, analogous to `ForeignAssets`/`TrustBackedAssets` registration, instead of deriving trust purely from the `AccountKey20` junction shape. Additionally, `deposit_asset_with_surplus`/`withdraw_asset_with_surplus` should verify that the amount being moved corresponds to an actual prior escrow/holding recorded for that specific contract and beneficiary, not just whatever value is present in the `AssetsInHolding` at execution time, mirroring the report's suggestion to add checks in `transferOut()`.

### Proof of Concept
1. Identify (or wait for) a real ERC20 contract `C` for which `TransfersCheckingAccount` holds a nonzero balance (e.g., from a prior legitimate reserve-transfer where `withdraw_asset_with_surplus` moved user funds into the checking account, or a prior deposit).
2. As any unprivileged user, submit an XCM program (via `pallet_xcm::execute` or crafted teleport) whose `Asset` uses `AssetId(Location::new(0, [AccountKey20 { key: C, network: None }]))` and `fun: Fungible(amount)`, where `amount` ≤ checking account's real balance of `C`, followed by `DepositAsset { assets: All, beneficiary: attacker_location }`.
3. `ERC20Matcher::matches_fungibles` accepts this without any registry check, returning `(C, amount)`. [5](#0-4) 
4. `ERC20Transactor::deposit_asset_with_surplus` executes `bare_call` as `TransfersCheckingAccount`, calling `C.transfer(attacker_eth_address, amount)`. [6](#0-5) 
5. The attacker receives `amount` of contract `C` from the checking account despite never having deposited it, confirming unbacked withdrawal of real token balance.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L236-266)
```rust
		let maybe = what
			.fungible_assets_iter()
			.next()
			.and_then(|asset| Matcher::matches_fungibles(&asset).ok());
		let (asset_contract_id, amount) = match maybe {
			Some(inner) => inner,
			None => return Err((what, MatchError::AssetNotHandled.into())),
		};
		let who = match AccountIdConverter::convert_location(who) {
			Some(inner) => inner,
			None => return Err((what, MatchError::AccountIdConversionFailed.into())),
		};
		// We need to map the 32 byte beneficiary account to a 20 byte account.
		let eth_address = T::AddressMapper::to_address(&who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
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
```
