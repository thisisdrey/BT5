### Title
Silent loss of ERC20 assets when `DepositAsset` targets multiple ERC20 holdings through `ERC20Transactor` - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::deposit_asset_with_surplus` is the `TransactAsset` implementation registered as the last item of the `AssetTransactors` tuple on Asset Hub (`cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:239-246`). When it is invoked with an `AssetsInHolding` that contains more than one ERC20 fungible asset, it transfers only the first one to the beneficiary and returns `Ok(...)`, silently discarding the rest, even though those tokens were already really transferred out of the user's balance into `ERC20TransfersCheckingAccount` during the preceding `WithdrawAsset` step. This is the same broken invariant as the external report's `recoverAsset`: value leaves accounted custody (the user's balance) and enters a pool (`checking_account`) whose outflow is not matched 1:1 against what was actually credited, so tokens can be permanently stranded/lost with no compensating accounting update, error, or `AssetsTrapped` event.

### Finding Description
`withdraw_asset_with_surplus` (erc20_transactor.rs:150-216) performs a real ERC20 `transfer` of the withdrawn amount from the user to `ERC20TransfersCheckingAccount`, and represents the credit purely as an ephemeral, non-persisted `Erc20Credit(u128)` placed into XCM `AssetsInHolding`: [1](#0-0) 

`deposit_asset_with_surplus` (erc20_transactor.rs:218-243) is explicitly documented to handle only a single fungible asset from the `AssetsInHolding` passed in, silently ignoring the rest: [2](#0-1) 

The `TransactAsset` tuple impl (`AssetTransactors = (FungibleTransactor, FungiblesTransactor, ForeignFungiblesTransactor, UniquesTransactor, ERC20Transactor)`) passes the *entire* remaining `AssetsInHolding` to each transactor and short-circuits on the first `Ok`: [3](#0-2) [4](#0-3) 

So if an XCM program withdraws two different ERC20 tokens (both are matched only by `ERC20Matcher`/`ERC20Transactor`, since `FungibleTransactor`/`FungiblesTransactor`/`ForeignFungiblesTransactor`/`UniquesTransactor` don't match ERC20 asset ids) and then issues a single `DepositAsset(Wild(AllCounted(n)), beneficiary)`, the executor calls `ERC20Transactor::deposit_asset_with_surplus` once with an `AssetsInHolding` holding both ERC20 credits. Only the first (per `fungible_assets_iter().next()`) is actually transferred out of `ERC20TransfersCheckingAccount` to the beneficiary; the call returns `Ok(surplus)`, and the remaining `Erc20Credit` value(s) are dropped along with the rest of `what` — they are never delivered, never trapped (`AssetsTrapped` is only emitted for assets remaining in the *executor's* holding register at the end of the program, not for assets consumed-then-discarded inside a `TransactAsset` call that reported success), and no error is raised. The corrupted value is the second/subsequent ERC20 balance already resident in `ERC20TransfersCheckingAccount`: it is real, spendable ERC20 balance with no XCM-holding representation and no on-chain bookkeeping tying it to anyone.

This directly mirrors the report's core defect: an operation moves real value into a shared custody account (`checking_account` ≈ the contract holding TRSY) while the corresponding internal accounting (`cumulativeFees` ≈ the XCM holding register / trapped-asset accounting) is not updated to reflect it, so the value becomes unaccounted and effectively unrecoverable/liable to loss.

### Impact Explanation
This breaks the "conserve value and settle exactly once" invariant for public XCM asset-transfer entrypoints. Any unprivileged holder of two or more ERC20-mapped tokens on Asset Hub can permanently lose all but one of those tokens by executing a normal, permissionless `pallet_xcm::execute`/incoming XCM program (no admin, governance, relayer, or validator involvement). Because the deposit silently "succeeds" (`Ok`), the caller has no on-chain signal that funds were lost, and the tokens are stuck in `ERC20TransfersCheckingAccount` indefinitely with no protocol-level mechanism to recover or reconcile them.

### Likelihood Explanation
Likelihood is high for anyone who understands the code path: it requires only building an ordinary XCM message with `WithdrawAsset` for two-plus ERC20 assets followed by a wildcard `DepositAsset`, executable via the permissionless `pallet_xcm::execute` extrinsic or via an incoming XCM message — no special privileges, timing, or race conditions needed.

### Recommendation
`ERC20Transactor::deposit_asset_with_surplus` must either (a) iterate over and deposit *all* fungible ERC20 assets present in `what`, returning any it cannot handle as `unspent` (per the `TransactAsset` tuple contract, so downstream transactors/trap-asset logic can process them), or (b) fail with an error (not `Ok`) whenever more than one asset is passed instead of only asserting in debug builds via `defensive_assert!`. The current `Ok(surplus)` return path that discards extra assets must be removed.

### Proof of Concept
1. Deploy/whitelist two ERC20 contracts `TokenA` and `TokenB` reachable through `assets_common::ERC20Matcher` (matched by `AccountKey20` junction), each with balance for `sender`.
2. `sender` submits (via `pallet_xcm::execute`, permissionless) an XCM program:
   - `WithdrawAsset((AccountKey20{TokenA}, amountA))`
   - `WithdrawAsset((AccountKey20{TokenB}, amountB))`
   - `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }`
3. Executor processes both `WithdrawAsset` instructions: `ERC20Transactor::withdraw_asset_with_surplus` is invoked for each, performing two real ERC20 `transfer` calls moving `amountA` of TokenA and `amountB` of TokenB from `sender` into `ERC20TransfersCheckingAccount`. Both `Erc20Credit`s are subsumed into the executor's holding register.
4. `DepositAsset` invokes the `AssetTransactors` tuple's `deposit_asset_with_surplus` with holding containing both credits. `FungibleTransactor`/`FungiblesTransactor`/`ForeignFungiblesTransactor`/`UniquesTransactor` return `AssetNotFound`/`Unimplemented` for ERC20 asset ids, so `ERC20Transactor` is invoked with the whole `AssetsInHolding`.
5. `ERC20Transactor::deposit_asset_with_surplus` picks only the first fungible asset (e.g. TokenA), transfers `amountA` from `checking_account` to `beneficiary`, and returns `Ok(surplus)`.
6. Result: `beneficiary` receives only TokenA; `amountB` of TokenB remains in `ERC20TransfersCheckingAccount` permanently, with `sender` no longer holding it, `beneficiary` never receiving it, and no error/trap event recorded anywhere on-chain — a real, unprivileged fund loss.

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
