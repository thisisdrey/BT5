### Title
`ERC20Transactor::deposit_asset_with_surplus` silently drops and permanently loses non-first fungible assets when `AssetsInHolding` contains more than one asset - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The external report's core broken invariant is: code that assumes a "pod"/holding-set contains exactly one token silently mishandles the case where it actually contains more than one, either reverting unexpectedly or (worse) dropping assets without accounting for them. The Snowbridge/Cumulus analog is `ERC20Transactor::deposit_asset_with_surplus`, the `TransactAsset` implementation used to move ERC20 tokens (via `pallet_revive`) during XCM execution. It explicitly documents that if the `AssetsInHolding` batch passed by the XCM executor contains more than one fungible asset, only the first is actually transferred to the beneficiary and the rest are "silently ignored" — yet the function still returns `Ok(surplus)`, causing the executor to treat the whole batch as successfully deposited.

### Finding Description
`deposit_asset_with_surplus` takes `what: AssetsInHolding` — a collection that can legitimately contain multiple fungible assets, since XCM instructions such as `DepositAsset { assets: Wild(All) | Wild(AllCounted(n)), .. }` collapse everything in the holding register matching a filter into a single `TransactAsset::deposit_asset`/`deposit_asset_with_surplus` call: [1](#0-0) 

The function only inspects the first fungible asset (`what.fungible_assets_iter().next()`), performs a single ERC20 `transfer` for that asset only, and — critically — returns `Ok(surplus)` on success: [2](#0-1) 

Because the trait contract for `deposit_asset`/`deposit_asset_with_surplus` treats an `Ok` return as "the entirety of `what` has been consumed/deposited" (see the trait default and tuple-combinator semantics, where `Ok(())` short-circuits and the whole `AssetsInHolding` is dropped without being returned to the caller): [3](#0-2) [4](#0-3) 

any second, third, etc. fungible ERC20 asset present in the same `AssetsInHolding` batch is neither transferred to the beneficiary nor returned as "unspent" to the executor's holding register — it is simply destroyed with no error, no trap, and no bookkeeping. The `defensive_assert!(what.len() == 1, ...)` only fires (and only in debug builds) as a soft warning; it does not prevent the silent loss in production and does not change control flow: [5](#0-4) 

This is the direct analog of the `LeverageManager` bug: code hard-codes a "single token" assumption about a collection (`selfLendingPod` vs. `AssetsInHolding`) that can legitimately hold multiple tokens, and the multi-token case is unhandled. Here it's arguably more severe than the original report because instead of a harmless revert, the excess assets are irrecoverably destroyed while the call still reports success.

### Impact Explanation
Any XCM program that ends up holding more than one ERC20 asset at the point a `DepositAsset` (or a reserve-transfer/teleport instruction that internally calls deposit) is executed against `ERC20Transactor` will have all but the first ERC20 asset permanently and silently burned instead of delivered to the beneficiary. This is a permanent user-fund loss with no attacker benefit and no recovery path (not even trapped-asset recovery, since the executor believes the deposit succeeded). This matches the "permanent user-fund lock/loss" impact category for chains that configure `ERC20Transactor` as (part of) their `AssetTransactor` for `pallet-revive`/ERC20 interop.

### Likelihood Explanation
Likelihood is Low-to-Medium and depends entirely on runtime configuration: a chain must (a) include `ERC20Transactor` in its `AssetTransactor` and (b) allow XCM programs (e.g., via `Wild(All)`/`Wild(AllCounted(n))` deposit filters, or a sequence combining `ExchangeAsset`, reserve deposits, and a final catch-all `DepositAsset`) to accumulate more than one ERC20-matched fungible asset in holding before depositing. Ordinary single-asset transfers are unaffected. The bug requires no privileged actor, no malicious relayer/validator, and no admin action — an ordinary unprivileged XCM sender constructing a normal multi-asset transfer/exchange program is sufficient to trigger loss of their own (or a beneficiary's) funds.

### Recommendation
Either:
1. Make `deposit_asset_with_surplus`/`deposit_asset` iterate over **all** fungible assets in `what`, performing an ERC20 transfer for each and only returning `Ok` once every asset has actually been moved; or
2. If only single-asset deposits are truly supported, return `Err((what, XcmError::FailedToTransactAsset(...)))` (returning the full unspent `what`) whenever `what.len() != 1`, so the XCM executor can trap the assets for later claim instead of silently discarding them. Replace the `defensive_assert!` with a hard runtime check that rejects the deposit rather than merely logging in debug builds.

### Proof of Concept
1. Configure a runtime with `ERC20Transactor` in its XCM `AssetTransactor`.
2. Craft an XCM program that, prior to a `DepositAsset` instruction, causes the holding register to contain two ERC20 assets matched by `Matcher` (e.g., via two prior `WithdrawAsset`/`ReserveAssetDeposited` instructions for `TokenA` and `TokenB`, both ERC20-backed).
3. Execute `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }`.
4. Observe: `ERC20Transactor::deposit_asset_with_surplus` receives `AssetsInHolding` with both `TokenA` and `TokenB`; only `TokenA` is transferred to the beneficiary via the ERC20 `transfer` call, `deposit_asset_with_surplus` returns `Ok(surplus)`, and `TokenB` is dropped when the `what: AssetsInHolding` value goes out of scope — the beneficiary never receives `TokenB`, and no error or trap event is emitted. [6](#0-5)

### Citations

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

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L79-100)
```rust
	/// Deposit the `what` asset in holding into the account of `who`.
	///
	/// Implementations should return `XcmError::FailedToTransactAsset` if deposit failed.
	fn deposit_asset(
		what: AssetsInHolding,
		_who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(), (AssetsInHolding, XcmError)> {
		Err((what, XcmError::Unimplemented))
	}

	/// Identical to `deposit_asset` but returning the surplus, if any.
	///
	/// Return the difference between the worst-case weight and the actual weight consumed.
	/// This can be zero most of the time unless there's some metering involved.
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		Self::deposit_asset(what, who, context).map(|()| Weight::zero())
	}
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L272-294)
```rust
	fn deposit_asset(
		mut what: AssetsInHolding,
		who: &Location,
		context: Option<&XcmContext>,
	) -> Result<(), (AssetsInHolding, XcmError)> {
		for_tuples!( #(
			match Tuple::deposit_asset(what, who, context) {
				Err((unspent, XcmError::AssetNotFound)) | Err((unspent, XcmError::Unimplemented)) => {
					what = unspent;
					// continue
				},
				r => return r,
			}
		)* );
		tracing::trace!(
			target: "xcm::TransactAsset::deposit_asset",
			?what,
			?who,
			?context,
			"did not deposit asset",
		);
		Err((what, XcmError::AssetNotFound))
	}
```
