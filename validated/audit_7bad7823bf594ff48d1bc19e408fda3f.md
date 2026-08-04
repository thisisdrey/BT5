### Title
Unbacked ERC20 asset credit via unvalidated attacker-controlled contract address in `ERC20Transactor` - (File: `cumulus/parachains/runtimes/assets/common/src/lib.rs`, `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The external report describes `GroupBuy.purchase()` calling an unvalidated, user-supplied `_market` contract with pooled funds and then trusting a self-reported return value (the "vault" address) from that same untrusted contract to decide whether the purchase succeeded — allowing an attacker to substitute their own contract and fabricate a "successful" outcome. The local analog is Asset Hub's `ERC20Transactor`, which treats *any* 20-byte address supplied inside an XCM `AssetId` Location as a legitimate ERC20 token contract, calls that attacker-chosen contract's `transfer()` function, and then trusts the ABI-decoded boolean return value from that very contract as proof that real value moved — with no registry/whitelist check that the contract is an approved asset.

### Finding Description
`ERC20Matcher` is defined as: [1](#0-0) 

It is built from `IsLocalAccountKey20`, which matches **any** Location of the form `(0, [AccountKey20 { key, .. }])`: [2](#0-1) 

There is no allow-list, no `pallet-assets`/foreign-asset registration check, and no verification that `key` corresponds to a real, previously-registered ERC20 contract — it is exactly the missing validation that the original report calls out for `_market`.

That arbitrary `key` (H160) becomes `asset_id`/`asset_contract_id`, which is then used directly as the target of a `pallet_revive::bare_call` that invokes `IERC20::transferCall` on it: [3](#0-2) 

The result is judged purely by decoding the return data of that same untrusted contract call: [4](#0-3) 

If `return_value.did_revert()` is `false` and the ABI-decoded bool is `true`, the transactor credits `AssetsInHolding` with an `Erc20Credit(amount)` for an arbitrary `amount` chosen by the caller — entirely because the attacker's own contract said so: [5](#0-4) 

`Erc20Credit` is a bare accounting wrapper with no real balance-check backing: [6](#0-5) 

`deposit_asset_with_surplus` follows the identical pattern on the deposit side, calling `transfer()` on the attacker-chosen `asset_contract_id` and trusting its self-reported success to release value to a beneficiary: [7](#0-6) 

`ERC20Transactor` is wired into the live `AssetTransactors` tuple used by the Asset Hub Westend XCM executor: [8](#0-7) 

This exactly mirrors the GroupBuy pattern: the "market"/contract address is fully attacker-chosen (here, embedded in an XCM `AssetId::AccountKey20` Location instead of a function argument), the pallet performs a value-relevant call into that untrusted contract, and it accepts the untrusted contract's own return value as the sole proof of a legitimate state transition (fund movement / asset crediting), instead of checking against an independent, trusted registry (the mitigation Tessera applied — checking the vault against `VaultRegistry` — has no counterpart here, e.g. no check against `pallet_assets`/a foreign-asset allow-list for the H160).

### Impact Explanation
Because `matches_fungibles` accepts *any* `AccountKey20` address, an attacker can deploy a trivial contract whose `transfer()` selector always returns `true` (or any encoding decoding to `true`) without moving any real value, then reference that contract's address as the `AssetId` in an XCM program (e.g. via `pallet_xcm::execute`, or asset legs of `WithdrawAsset`/`DepositAsset`/`TransferAsset` instructions processed by the XCM executor's `TransactAsset` implementation). This lets the runtime credit `AssetsInHolding` with an arbitrary amount of "ERC20" value backed by nothing, which can then be moved through further XCM instructions (e.g. `DepositAsset` to the attacker's own account, or fed into `pallet_asset_conversion` pools) to be exchanged for or presented as real, valuable assets. This is an unbacked-mint / value-conservation violation matching the "theft or unbacked mint" impact category for the Polkadot SDK program.

### Likelihood Explanation
The path only requires an unprivileged user to deploy a `pallet-revive` contract (a normal, permissionless action) and submit an XCM program referencing that contract's address as an `AssetId` — no validator, governance, relayer, or leaked-key assumption is needed. The matcher performs zero registry validation before invoking `bare_call`, so nothing currently blocks this path from being exercised by any signed account with enough balance to submit the extrinsic and deploy the contract.

### Recommendation
Restrict `ERC20Matcher`/`IsLocalAccountKey20` (or add an additional filter layered into the `MatchedConvertedConcreteId`) so that only H160 addresses registered in an explicit, governance-controlled allow-list/registry (e.g., mirroring how `ForeignAssetsConvertedConcreteId` restricts based on registered foreign asset Locations) are accepted as valid ERC20 asset ids for `ERC20Transactor`, instead of accepting any `AccountKey20` Location unconditionally.

### Proof of Concept
1. Attacker deploys a `pallet-revive` contract `Fake` implementing `IERC20` whose `transfer(address,uint256)` always returns `abi.encode(true)` without changing any real balances.
2. Attacker's local account has the `Fake` contract's `H160` address `key`.
3. Attacker calls `pallet_xcm::execute` (or crafts a reserve/local XCM) with an instruction sequence containing `WithdrawAsset(Asset { id: AssetId(Location::new(0, [AccountKey20{ key, network: None }])), fun: Fungible(LARGE_AMOUNT) }, ...)`.
4. The XCM executor routes this to `ERC20Transactor::withdraw_asset_with_surplus`; `ERC20Matcher::matches_fungibles` accepts `key` unconditionally, and `bare_call` invokes `Fake.transfer(checking_address, LARGE_AMOUNT)`, which returns `true` without moving value; `AssetsInHolding` is credited with `Erc20Credit(LARGE_AMOUNT)`.
5. Follow-up instructions (`DepositAsset` to the attacker's own account, or supplying this holding into an asset-conversion pool leg) let the attacker realize or trade the fabricated balance for real value, without any real ERC20 tokens ever having moved.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-155)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L157-160)
```rust
/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L79-107)
```rust
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

impl UnsafeManualAccounting<u128> for Erc20Credit {
	fn saturating_subsume(&mut self, mut other: Box<dyn ImbalanceAccounting<u128>>) {
		let amount = other.forget_imbalance();
		self.0 = self.0.saturating_add(amount);
	}
}

impl ImbalanceAccounting<u128> for Erc20Credit {
	fn amount(&self) -> u128 {
		self.0
	}
	fn saturating_take(&mut self, amount: u128) -> Box<dyn ImbalanceAccounting<u128>> {
		let new = self.0.min(amount);
		self.0 = self.0 - new;
		Box::new(Erc20Credit(new))
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-181)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-216)
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
			}
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err(XcmError::FailedToTransactAsset("ERC20 contract execution errored"))
		}
	}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L248-266)
```rust
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
