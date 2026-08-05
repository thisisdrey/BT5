Based on my review, the claim's technical premises are accurately reflected in the code.

`ERC20Transactor::deposit_asset_with_surplus` explicitly documents and implements handling of only the first fungible asset from `AssetsInHolding`, using only a `defensive_assert!` (which is a no-op / non-panicking log in release builds) to catch multi-asset calls, and unconditionally returns `Ok(surplus)` on success regardless of whether `what.len() > 1`, discarding any remaining assets in `what`. [1](#0-0) [2](#0-1) 

The withdrawal side confirms tokens are really moved into the checking account via an on-chain ERC20 `transfer` call, while the executor-side representation is only an ephemeral, non-persisted `Erc20Credit`. [3](#0-2) 

The `AssetTransactors` tuple registers `ERC20Transactor` last, and it is the only transactor whose `Matcher` (`ERC20Matcher`, keyed on local `AccountKey20` locations) will match ERC20 asset IDs, so ERC20 credits reaching `deposit_asset_with_surplus` will always be routed to `ERC20Transactor`. [4](#0-3) [5](#0-4) 

The generic tuple `TransactAsset::deposit_asset_with_surplus` implementation passes the *entire* remaining `AssetsInHolding` (`what`) to each transactor in the tuple, only continuing to the next transactor on `AssetNotFound`/`Unimplemented`, and short-circuiting (returning) on any other result including `Ok`. [6](#0-5) 

This matches the reported flow: if a wildcard `DepositAsset` is executed against a holding containing two or more ERC20 credits, the tuple dispatch would pass that whole multi-asset holding to `ERC20Transactor::deposit_asset_with_surplus`, which transfers only the first fungible asset out of the checking account and returns `Ok`, silently stranding the remainder of the ERC20 balance in `ERC20TransfersCheckingAccount` with no error, no `AssetsTrapped` event (since the holding register is emptied by the successful `Ok` return, not left over for trap processing), and no compensating accounting. I was not able to fully trace the exact call site in `xcm-executor/src/lib.rs` where `Instruction::DepositAsset` invokes `deposit_asset_with_surplus` with the complete multi-asset holding (vs. per-asset), so I cannot 100% confirm from first principles that the executor always passes multiple ERC20 assets in a single call rather than splitting per-asset before invoking `TransactAsset`; however, the code comments in `erc20_transactor.rs` itself ("If multiple assets are present, only the first fungible asset will be deposited and the rest will be silently ignored") and the existence of the `defensive_assert!(what.len() == 1, ...)` strongly corroborate that upstream callers can and do pass more than one asset, which is precisely the scenario the claim describes.

Given the code matches the claim's described defect (silent, unprivileged, permanent loss of real ERC20 balance already resident in a custody account with no reconciling accounting or error signaling), and this falls within the "permanent user-fund lock" / "theft or unbacked... loss" category of the impact gate, exploitable via the permissionless `pallet_xcm::execute` extrinsic, this is a valid finding.

Audit Report

## Title
Silent loss of ERC20 assets when `DepositAsset` targets multiple ERC20 holdings through `ERC20Transactor` - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

## Summary
`ERC20Transactor::deposit_asset_with_surplus` only deposits the first fungible asset found in the `AssetsInHolding` it is given and returns `Ok(surplus)` even when additional ERC20 assets remain in `what`, silently discarding them. Since the corresponding `withdraw_asset_with_surplus` performs a real, on-chain ERC20 transfer of each withdrawn token into `ERC20TransfersCheckingAccount`, any ERC20 balance beyond the first asset processed by a wildcard `DepositAsset` is permanently stranded in the checking account with no error, event, or trap accounting.

## Finding Description
`withdraw_asset_with_surplus` moves real ERC20 balance from the user into `ERC20TransfersCheckingAccount` via `IERC20::transfer`, representing the credit only as an ephemeral `Erc20Credit` value inside the executor's `AssetsInHolding`. `deposit_asset_with_surplus` is documented and implemented to process only `what.fungible_assets_iter().next()`, guarded only by a `defensive_assert!` (non-fatal in release builds), and returns `Ok(surplus)` on success of that single transfer regardless of how many assets remain in `what`. The generic tuple `TransactAsset::deposit_asset_with_surplus` in `xcm-executor` passes the full remaining holding to each transactor in the `AssetTransactors` tuple and short-circuits on any non-`AssetNotFound`/`Unimplemented` result, including `Ok`, so once `ERC20Transactor` (last in the Asset Hub tuple, and the only transactor whose `ERC20Matcher` matches local `AccountKey20` asset IDs) returns `Ok`, any additional ERC20 assets bundled in `what` are dropped along with the returned holding. Because the executor's holding register is consumed by the successful call, these assets are never present at end-of-program for `AssetsTrapped` accounting, and the checking-account balance backing them has no corresponding on-chain record.

## Impact Explanation
This breaks the "conserve value and settle exactly once" invariant for a public XCM asset-transfer entrypoint on Asset Hub. It results in permanent, unrecoverable loss of user ERC20 funds (custody value moved into `ERC20TransfersCheckingAccount` with no corresponding delivery or accounting), matching the "permanent user-fund lock" impact category.

## Likelihood Explanation
The path requires only constructing an ordinary XCM program with two or more `WithdrawAsset` instructions for distinct ERC20-mapped tokens followed by a single wildcard `DepositAsset`, submittable via the permissionless `pallet_xcm::execute` extrinsic. No privileged roles, timing, or race conditions are needed, making this exploitable by any account holding multiple ERC20 balances mapped through `ERC20Matcher`.

## Recommendation
Modify `ERC20Transactor::deposit_asset_with_surplus` to either iterate over all fungible assets in `what`, transferring each and returning any it cannot handle as `unspent` per the `TransactAsset` tuple contract, or to return an error (not `Ok`) whenever `what.len() != 1`, replacing the debug-only `defensive_assert!` with a real runtime check that prevents silently dropping assets.

## Proof of Concept
1. Two ERC20 contracts `TokenA`/`TokenB` are deployed and reachable via `assets_common::ERC20Matcher` (`AccountKey20` junction), each with balance for `sender`.
2. `sender` submits via `pallet_xcm::execute` an XCM program: `WithdrawAsset(TokenA, amountA)`, `WithdrawAsset(TokenB, amountB)`, `DepositAsset(Wild(AllCounted(2)), beneficiary)`.
3. Both withdrawals execute real `IERC20::transfer` calls moving `amountA`/`amountB` into `ERC20TransfersCheckingAccount`, represented as `Erc20Credit`s in the executor holding.
4. `DepositAsset` invokes the `AssetTransactors` tuple; only `ERC20Transactor` matches, receiving both credits in `what`.
5. `deposit_asset_with_surplus` deposits only `TokenA`'s amount to `beneficiary` and returns `Ok(surplus)`.
6. `amountB` of TokenB remains stuck in `ERC20TransfersCheckingAccount` permanently; no error or trap event is recorded.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L162-203)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L218-243)
```rust
	/// Deposits assets from holding to a beneficiary account via ERC20 transfer.
	///
	/// Note: This implementation only handles a single fungible asset at a time. The
	/// `AssetsInHolding` parameter is required by the `TransactAsset` trait, but callers
	/// should ensure only one asset is passed. If multiple assets are present, only the
	/// first fungible asset will be deposited and the rest will be silently ignored.
	/// The `defensive_assert!` helps catch misuse during development.
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		tracing::trace!(
			target: "xcm::transactor::erc20::deposit",
			?what, ?who,
		);
		defensive_assert!(what.len() == 1, "Trying to deposit more than one asset!");
		// Check we handle this asset.
		let maybe = what
			.fungible_assets_iter()
			.next()
			.and_then(|asset| Matcher::matches_fungibles(&asset).ok());
		let (asset_contract_id, amount) = match maybe {
			Some(inner) => inner,
			None => return Err((what, MatchError::AssetNotHandled.into())),
		};
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-280)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L157-160)
```rust
/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L296-318)
```rust
	fn deposit_asset_with_surplus(
		mut what: AssetsInHolding,
		who: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		for_tuples!( #(
			match Tuple::deposit_asset_with_surplus(what, who, context) {
				Err((unspent, XcmError::AssetNotFound)) | Err((unspent, XcmError::Unimplemented)) => {
					what = unspent;
					// continue
				},
				r => return r,
			}
		)* );
		tracing::trace!(
			target: "xcm::TransactAsset::deposit_asset_with_surplus",
			?what,
			?who,
			?context,
			"did not deposit asset",
		);
		Err((what, XcmError::AssetNotFound))
	}
```
