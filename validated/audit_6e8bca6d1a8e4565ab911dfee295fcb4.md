Audit Report

## Title
`ERC20Transactor::deposit_asset_with_surplus` silently drops all but the first fungible asset in a multi-asset `AssetsInHolding` deposit, permanently destroying the discarded value while reporting success - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::deposit_asset_with_surplus` takes the full `what: AssetsInHolding` by value but only inspects and transfers `what.fungible_assets_iter().next()`; any additional fungible entries in the same holding are never transferred, never returned as unspent, and never trapped — they are unconditionally dropped when the function returns `Ok(surplus)`, per its own doc comment ("the rest will be silently ignored") [1](#0-0) . The only safeguard, `defensive_assert!(what.len() == 1, ...)`, is a no-op in release/production builds and provides no runtime protection [2](#0-1) .

## Finding Description
`deposit_asset_with_surplus` extracts only the first fungible asset via `fungible_assets_iter().next()`, matches it, performs a single ERC20 `transfer()` call through `pallet_revive::Pallet::<T>::bare_call`, and on a successful contract call returns `Ok(surplus)` without ever re-emitting, transferring, or trapping the rest of `what` [3](#0-2) . The trait's default `TransactAsset::deposit_asset` calls `deposit_asset_with_surplus` directly with the whole `what` [4](#0-3) , so any caller of `deposit_asset`/`deposit_asset_with_surplus` (not just the XCM executor's specific `DepositAsset` instruction path) is exposed if it does not itself guarantee a single-asset holding.

The claim's assertion that the standard executor path is protected is confirmed: `deposit_assets_with_retry` explicitly pre-splits holdings via `into_per_asset_holdings()` before calling the transactor [5](#0-4) , which limits exposure through that specific call site.

However, I checked the two call sites the claim names as exploitable and found the claim overstates one of them. `deposit_or_burn_fee` in `polkadot/xcm/xcm-builder/src/fee_handling.rs` iterates `fee.fungible.into_iter()` and constructs a **single-asset** `AssetsInHolding` via `AssetsInHolding::new_from_fungible_credit` for each asset id before calling `AssetTransactor::deposit_asset` [6](#0-5)  — this path is already per-asset split and is **not** vulnerable as described. I was unable to fully verify the second cited call site in `cumulus/primitives/utility/src/lib.rs` within the available iterations; its `deposit_asset` usages were located but not conclusively reviewed for whether they pass multi-asset holdings.

Independent of those two specific examples, the underlying defect in `ERC20Transactor::deposit_asset_with_surplus` itself is real and verified directly in the transactor code: it is a `TransactAsset` implementation that can be composed into arbitrary tuples (`AssetTransactors`) and invoked by any code path — current or future, including custom `TransactAsset` compositions, `transfer_asset`'s withdraw/deposit fallback, or any instruction implementation that doesn't pre-split per-asset-id — that calls `deposit_asset`/`deposit_asset_with_surplus` with more than one fungible ERC20-matched asset in a single `AssetsInHolding`. `ERC20Matcher` accepts any local `AccountKey20` location as a valid asset id with no allow-list [7](#0-6) , so no privileged action is needed to construct qualifying assets — an attacker only needs two ERC20 contracts deployed via `pallet_revive` and an XCM path that bundles both into one deposit call.

## Impact Explanation
When triggered, this causes permanent, unrecoverable loss of the non-first asset's value: it is neither delivered to the beneficiary nor returned to the caller nor trapped by `AssetTrap`, while the call reports `Ok`. This is a conservation-of-value violation on contract-held/user-held value, matching the "permanent user-fund lock" / fund-loss impact category for the Polkadot SDK impact gate.

## Likelihood Explanation
Exploitation requires no privileged actor — only an unprivileged party deploying ERC20 contracts and finding/triggering a code path that calls `AssetTransactor::deposit_asset` (or `deposit_asset_with_surplus`) with a multi-asset `AssetsInHolding` matched by the permissive `ERC20Matcher`. The standard XCM executor `DepositAsset` instruction path is protected by `into_per_asset_holdings()`, and at least one of the two specific call sites named in the claim (`fee_handling.rs`) is also protected since it already splits per fungible asset before calling `deposit_asset`. This narrows the claim's stated likelihood: the bug is real and unguarded at the transactor level (`defensive_assert!` is inert in production), but a concretely demonstrated *reachable* multi-asset call path into this specific transactor within the current codebase was not fully confirmed for the second cited call site (`cumulus/primitives/utility/src/lib.rs`), leaving exploitability contingent on either an unverified path or future/alternate composition of `AssetTransactors`.

## Recommendation
`deposit_asset_with_surplus`/`deposit_asset` in `ERC20Transactor` should either reject calls with more than one asset outright (returning `Err` with the full, untouched `what`), or iterate and process every fungible entry in `what`, returning any unhandled/failed entries as unspent `AssetsInHolding`. Replace the `defensive_assert!` with a hard `ensure!`/early-return check so both debug and release builds are protected regardless of caller assumptions.

## Proof of Concept
1. Deploy two ERC-20 contracts `A` and `B` via `pallet-revive`.
2. Construct an `AssetsInHolding` containing `Fungible(addrA, amountA)` and `Fungible(addrB, amountB)`, both matched by `ERC20Matcher`.
3. Call `ERC20Transactor::deposit_asset_with_surplus` directly (or via any caller that does not pre-split per-asset-id) with this holding.
4. Observe only `amountA` is transferred via the `IERC20::transferCall`; the function returns `Ok(surplus)`; `amountB` is dropped when `what` goes out of scope, with no error, no trap event, and no way to recover it — confirming the code-level defect at `erc20_transactor.rs:225-306`.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L218-224)
```rust
	/// Deposits assets from holding to a beneficiary account via ERC20 transfer.
	///
	/// Note: This implementation only handles a single fungible asset at a time. The
	/// `AssetsInHolding` parameter is required by the `TransactAsset` trait, but callers
	/// should ensure only one asset is passed. If multiple assets are present, only the
	/// first fungible asset will be deposited and the rest will be silently ignored.
	/// The `defensive_assert!` helps catch misuse during development.
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-286)
```rust
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
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L94-100)
```rust
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		Self::deposit_asset(what, who, context).map(|()| Weight::zero())
	}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1853-1873)
```rust
	fn deposit_assets_with_retry(
		to_deposit: AssetsInHolding,
		beneficiary: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, XcmError> {
		let mut total_surplus = Weight::zero();
		let mut failed_deposits = AssetsInHolding::new();

		// First pass: try to deposit each asset; failures go to retry.
		for single in to_deposit.into_per_asset_holdings() {
			match Config::AssetTransactor::deposit_asset_with_surplus(single, beneficiary, context)
			{
				Ok(surplus) => total_surplus.saturating_accrue(surplus),
				Err((unspent, _)) => {
					// First-pass failure: keep for retry. A subsequent deposit in the same
					// pass may create the destination account (by satisfying ED), allowing
					// the retry pass to succeed for assets that fall here.
					failed_deposits.subsume_assets(unspent);
				},
			}
		}
```

**File:** polkadot/xcm/xcm-builder/src/fee_handling.rs (L109-125)
```rust
pub fn deposit_or_burn_fee<AssetTransactor: TransactAsset>(
	fee: AssetsInHolding,
	context: Option<&XcmContext>,
	dest: Location,
) {
	// If `fee` contains multiple assets, we need to process one fungible asset at a time.
	// Non-fungibles are ignored.
	for (asset_id, credit) in fee.fungible.into_iter() {
		let fee_asset = AssetsInHolding::new_from_fungible_credit(asset_id, credit);
		if let Err((unspent, e)) = AssetTransactor::deposit_asset(fee_asset, &dest, context) {
			tracing::trace!(
				target: "xcm::fees",
				"`AssetTransactor::deposit_asset` returned error: {e:?}. \
				Dropping fee: {unspent:?} (might be burned).",
			);
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
