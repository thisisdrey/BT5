### Title
`ERC20Transactor` initiates ERC20 transfers without a zero-amount guard, causing XCM asset transfers to revert for weird ERC20 tokens - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` build and dispatch an ERC20 `transfer(to, value)` call via `pallet_revive::Pallet::<T>::bare_call` using whatever `amount` the XCM `MatchesFungibles` matcher returns, without ever checking that `amount != 0`. Some ERC20 tokens revert on zero-value transfers (the same class of "weird ERC20" issue as the LEND token cited in the Astaria report). When such a zero-value transfer reverts, the XCM instruction fails with `XcmError::FailedToTransactAsset`, causing the whole XCM program (and any assets already withdrawn/held) to fail to settle as intended.

### Finding Description
`withdraw_asset_with_surplus` (used for `WithdrawAsset`) does: [1](#0-0) 
It calls `Matcher::matches_fungibles(what)` to get `(asset_id, amount)` and unconditionally encodes and dispatches `IERC20::transferCall { to: checking_address, value: EU256::from(amount) }` — there is no check that `amount > 0` before making the ERC20 call.

Similarly, `deposit_asset_with_surplus` (used for `DepositAsset`) does the mirror operation from the checking account to the beneficiary: [2](#0-1) 
Again, `amount` (which can legitimately be `0`, e.g. from a `Fungible(0)` XCM asset or a `WildAsset`/partial-transfer computation that yields zero) is passed straight into the `transferCall` with no zero check.

If `asset_id` corresponds to a "weird" ERC20 contract that reverts on `transfer(to, 0)` (this exact class of token is explicitly acknowledged elsewhere in this same repository — see the Snowbridge outbound-queue-v2 XCM converter, which explicitly rejects zero-value asset transfers with a dedicated error rather than letting them reach a token contract or transactor): [3](#0-2) 
the `bare_call` will return `did_revert() == true`, and the transactor converts that into `XcmError::FailedToTransactAsset("ERC20 contract reverted")`: [4](#0-3) [5](#0-4) 

There is no existing guard against this in the xcm-executor's generic `TransactAsset` trait either (no `is_zero`/`ZeroAssetTransfer` handling was found in `polkadot/xcm/xcm-executor`), so nothing upstream filters zero-value fungible assets before they reach this transactor. This is the same root-cause pattern as M-15 in the Astaria report: an unconditional `transfer(..., amount)` call is made even when `amount == 0`, and some ERC20 implementations revert on that call, causing the whole flow to fail.

### Impact Explanation
Any XCM program that routes an ERC20-backed asset (via `ERC20Transactor`, e.g. registered on Asset Hub Westend per `pallet-revive`/ERC20 asset support) through this transactor with a zero fungible amount for that asset — whether due to a wildcard/partial deposit that resolves to zero, a fee/remainder computation that leaves zero of that asset, or a maliciously/accidentally crafted XCM message specifying `Fungible(0)` for an ERC20 asset id — will cause the transact-asset call to revert if the underlying ERC20 token reverts on zero-value transfers. This can:
- Cause the whole XCM message (including other, unrelated assets bundled in the same message) to fail execution, since `TransactAsset::withdraw_asset`/`deposit_asset` errors propagate as instruction failures in the XCM executor.
- Leave assets stuck: in `withdraw_asset_with_surplus`, if other assets in the same instruction were already checked out/withdrawn while this one reverts, or in `deposit_asset_with_surplus`, funds already pulled into the `TransfersCheckingAccount` cannot be delivered to the beneficiary, resulting in a locked/stuck-fund state consistent with the "permanent user-fund lock" impact class.
- Enable a state where the affected ERC20 asset can never be moved through this transactor at all (a permanent denial of transacting that specific asset), because any XCM path that ever produces a zero-amount leg for that asset will always revert.

### Likelihood Explanation
The likelihood is moderate: it does not require a privileged actor, malicious relayer, or governance action — any unprivileged party constructing or triggering an XCM message that results in a `Fungible(0)` amount for an ERC20-backed asset (e.g., wildcard `DepositAsset`/`AllCounted` resolving to zero for one asset among several, or fee/change computations reaching zero) can trigger this path. It is gated only by the operational fact that the specific ERC20 contract registered for that asset must revert on zero-value transfers — a known, documented behavior of some real-world ERC20 tokens (e.g., LEND). Given Asset Hub explicitly supports arbitrary ERC20 contracts as asset ids via this exact transactor (as introduced for ERC20 support on Asset Hub Westend), the codebase cannot guarantee only "well-behaved" ERC20s are ever registered.

### Recommendation
In both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, short-circuit and skip (or return success/no-op) when `amount == 0`, mirroring the existing `ZeroAssetTransfer`-style guard already used in the Snowbridge outbound-queue-v2 XCM converter, instead of unconditionally dispatching `IERC20::transferCall` with a zero `value`.

### Proof of Concept
1. Register an ERC20 contract as an XCM asset id via `ERC20Transactor` on a chain configured like `asset-hub-westend` (per `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`), where the ERC20 contract's `transfer` function reverts when `value == 0` (mirroring LEND-style weird-ERC20 behavior).
2. Construct an XCM program whose asset filter/wildcard resolution (or an explicit `Fungible(0)` asset) causes `Matcher::matches_fungibles` to return `amount == 0` for that ERC20 asset id, e.g. a `WithdrawAsset`/`DepositAsset` instruction touching that asset alongside others where the actual remaining/matched amount for that asset is zero.
3. Execute the XCM message through the executor; `withdraw_asset_with_surplus`/`deposit_asset_with_surplus` (lines 159-181 / 236-266 of `erc20_transactor.rs`) calls `pallet_revive::Pallet::<T>::bare_call` with `IERC20::transferCall { value: 0 }`.
4. Observe `return_value.did_revert() == true`, producing `XcmError::FailedToTransactAsset("ERC20 contract reverted")` and failing the enclosing XCM instruction/program, demonstrating the fund-lock / transaction-failure condition without any privileged actor involved.

### Citations

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-189)
```rust
		if let Ok(return_value) = result {
			tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?return_value, "Return value by withdraw_asset");
			if return_value.did_revert() {
				tracing::debug!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract reverted");
				Err(XcmError::FailedToTransactAsset("ERC20 contract reverted"))
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-274)
```rust
		if let Ok(return_value) = result {
			tracing::trace!(target: "xcm::transactor::erc20::deposit", ?return_value, "Return value");
			if return_value.did_revert() {
				tracing::debug!(target: "xcm::transactor::erc20::deposit", "Contract reverted");
				Err((what, XcmError::FailedToTransactAsset("ERC20 contract reverted")))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L985-1019)
```rust
#[test]
fn xcm_converter_convert_with_zero_amount_asset_yields_zero_asset_transfer() {
	let network = BridgedNetwork::get();

	let token_address: [u8; 20] = hex!("1000000000000000000000000000000000000000");
	let beneficiary_address: [u8; 20] = hex!("2000000000000000000000000000000000000000");

	let assets: Assets = vec![Asset {
		id: AssetId(AccountKey20 { network: None, key: token_address }.into()),
		fun: Fungible(0),
	}]
	.into();
	let filter: AssetFilter = Wild(WildAsset::AllCounted(1));
	let fee_asset: Asset = Asset { id: AssetId(Here.into()), fun: Fungible(1000) }.into();

	let message: Xcm<()> = vec![
		WithdrawAsset(fee_asset.clone().into()),
		PayFees { asset: fee_asset },
		WithdrawAsset(assets.clone()),
		AliasOrigin(Location::new(1, [GlobalConsensus(Polkadot), Parachain(1000)])),
		DepositAsset {
			assets: filter,
			beneficiary: AccountKey20 { network: None, key: beneficiary_address }.into(),
		},
		SetTopic([0; 32]),
	]
	.into();
	let mut converter =
		XcmConverter::<MockTokenIdConvert, (), EverythingBut<Equals<AssetHubLocation>>>::new(
			&message, network,
		);

	let result = converter.convert();
	assert_eq!(result.err(), Some(XcmConverterError::ZeroAssetTransfer));
}
```
