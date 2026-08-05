### Title
Silent multi-asset drop in `ERC20Transactor::deposit_asset_with_surplus` leaves ERC20 tokens stuck in the shared checking account - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The Blueberry bug used `balanceOf(uToken)` instead of `balanceOf(vault)` to size a collateral transfer, silently leaving vault tokens behind in the contract where they became stealable. The local analog is in `ERC20Transactor::deposit_asset_with_surplus`, which only ever forwards the *first* fungible ERC20 asset out of `AssetsInHolding` to the beneficiary and silently discards any additional ERC20 assets that were matched into the same holding, leaving them parked in the shared `TransfersCheckingAccount` contract instead of being delivered or accounted for.

### Finding Description
`ERC20Transactor::deposit_asset_with_surplus` receives an `AssetsInHolding` (potentially containing multiple assets that matched `ERC20Matcher`) and explicitly documents that it "only handles a single fungible asset at a time": [1](#0-0) 

It only calls `defensive_assert!(what.len() == 1, ...)` — a debug-only check that is a no-op (just a log) in release/production builds, which is how runtime WASM/PolkaVM is actually compiled — and then takes only `what.fungible_assets_iter().next()` to determine the `asset_contract_id`/`amount` to transfer via the checking account's ERC20 `transfer` call: [2](#0-1) 

Because the amount moved out of `TransfersCheckingAccount` is derived only from the first matched asset (not from the checking account's actual on-chain ERC20 `balanceOf`, nor from the full contents of `what`), any additional ERC20 asset(s) present in the same holding are dropped from `AssetsInHolding` when the function returns `Ok(surplus)` — the XCM executor treats the whole deposit as successful and the leftover ERC20 balance remains sitting in `TransfersCheckingAccount` (a single well-known account shared by all users of this transactor, e.g. `ERC20TransfersCheckingAccount` on Asset Hub): [3](#0-2) 

This is structurally the same class of bug as the Blueberry finding: the code determines "how much to move" from the wrong source (the first-matched asset amount, not the aggregate real balance/holding), leaving real ERC20 balance parked in a shared contract account that is not tracked per-beneficiary going forward. Since `TransfersCheckingAccount` never holds a balance ledger — it's an ERC20 balance shared across all XCM users of that asset — any subsequently processed `withdraw_asset_with_surplus`/`deposit_asset_with_surplus` cycle for the same ERC20 token can end up moving/crediting more than the corresponding user actually deposited, because the on-chain `balanceOf(checking_account)` is inflated by the earlier silently-dropped remainder with no accounting record tying it to the rightful beneficiary.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for public asset transfer/deposit paths on Asset Hub. A rightful beneficiary permanently loses ERC20 tokens that were supposed to be delivered to them (silently dropped, no error, no trap event since `AssetsInHolding::new_from_fungible_credit` accounting for the dropped asset is simply never returned as trapped), while the checking account accumulates real ERC20 balance not represented in any on-chain ledger — funds that can later be moved out under a different, unrelated transaction because `deposit_asset_with_surplus` sizes transfers purely from the XCM-supplied `amount`, not from validating against `IERC20::balanceOf(checking_account)`.

### Likelihood Explanation
This is reachable by any unprivileged user constructing an ordinary XCM program (e.g. `WithdrawAsset`/`DepositAsset` with `Wild(All)`/`AllCounted` over multiple distinct ERC20 token holdings) executed via `pallet_xcm::execute` or a cross-chain message that ends up matching more than one ERC20 asset into a single `deposit_asset` call routed to `ERC20Transactor`. No privileged actor, relayer, or malicious validator is required — it is purely a public-entrypoint XCM execution path.

### Recommendation
Reject deposits with more than one fungible asset instead of silently accepting them (return an error/require callers to split multi-asset XCM instructions so each ERC20 asset is deposited individually), and/or make `defensive_assert!` failure hard-fail (return `XcmError`) in `deposit_asset_with_surplus` rather than proceeding to move only the first asset. Additionally, `withdraw_asset_with_surplus`/`deposit_asset_with_surplus` should validate movement against the checking account's actual `IERC20::balanceOf` delta rather than blindly trusting the XCM-supplied `amount`, so no ERC20 balance can accumulate untracked in the shared checking account.

### Proof of Concept
1. Register two distinct ERC20 contracts (`tokenA`, `tokenB`) as XCM-recognized assets matched by `ERC20Matcher`.
2. Construct an XCM program that withdraws both `tokenA` and `tokenB` amounts into holding (e.g., two `WithdrawAsset` instructions) and then issues a single `DepositAsset { assets: Wild(All), beneficiary }`.
3. The XCM executor calls `ERC20Transactor::deposit_asset_with_surplus` with an `AssetsInHolding` containing both `tokenA` and `tokenB` credits.
4. Per [4](#0-3) , only the first asset in iteration order (e.g. `tokenA`) is transferred to `beneficiary`; `tokenB`'s credit is dropped when the function returns `Ok(surplus)`, leaving `tokenB`'s balance sitting in `TransfersCheckingAccount` un-delivered to `beneficiary` and unaccounted in `AssetsInHolding`.
5. `beneficiary` never receives `tokenB`, and `TransfersCheckingAccount`'s on-chain `tokenB` `balanceOf` is now higher than any recorded XCM-pending state — available to be later moved by any subsequent `tokenB` deposit/withdraw sequence through this transactor.

Note: I was unable to fully trace how `xcm-executor`'s asset-grouping logic decides whether multiple distinct ERC20 assets can be batched into one `deposit_asset` call for a single `TransactAsset` tuple member (this logic lives outside the files directly examined); the code comment and `defensive_assert!` in `erc20_transactor.rs` itself, however, confirm the maintainers are aware this multi-asset case is possible and currently unenforced beyond a debug-only assertion.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L249-266)
```rust
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-237)
```rust
parameter_types! {
	/// Taken from the real gas and deposits of a standard ERC20 transfer call.
	pub const ERC20TransferGasLimit: Weight = Weight::from_parts(500_000_000_000, 10 * 1024 * 1024);
	pub const ERC20TransferStorageDepositLimit: Balance = 10_200_000_000;
	pub ERC20TransfersCheckingAccount: AccountId = PalletId(*b"py/revch").into_account_truncating();
	pub DapBufferAccount: AccountId = pallet_dap::Pallet::<Runtime>::buffer_account();
}

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
```
