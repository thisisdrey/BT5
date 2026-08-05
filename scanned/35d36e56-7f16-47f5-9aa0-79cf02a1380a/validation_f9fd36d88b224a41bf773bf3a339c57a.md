## Analysis

The TOFT bug's core primitive: **a public/attacker-reachable function accepts an unvalidated token-contract address plus amount from message data and calls `transfer()` on that arbitrary ERC20 contract from a shared, contract-controlled account to an attacker-chosen beneficiary — with no check that this specific token/amount is actually backed by what the attacker itself deposited.**

The closest local analog is `ERC20Transactor` in `assets-common`, wired into Asset Hub's XCM executor as one of the `AssetTransactors`.

### Title
Unvalidated attacker-chosen ERC20 contract address in `ERC20Transactor::deposit_asset_with_surplus` allows draining the shared checking account of any ERC20 held by pallet-revive - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` implements `TransactAsset` for ERC20 tokens managed by `pallet-revive` on Asset Hub. `ERC20Matcher` maps any XCM `Asset` whose `id` is `{parents:0, interior: AccountKey20{key,...}}` directly to the smart-contract address `key` — i.e. **the attacker's own XCM `Asset.id` field chooses which ERC20 contract gets called**, exactly like `tapSendData.tapOftAddress` in the TOFT report. [1](#0-0) [2](#0-1) 

### Finding Description
`withdraw_asset_with_surplus` resolves `(asset_id, amount)` from the attacker-supplied `Asset` via `Matcher::matches_fungibles`, then makes the *origin* call `IERC20::transfer(checking_account, amount)` on whatever contract address `asset_id` resolves to. [1](#0-0) 

`deposit_asset_with_surplus` then resolves `(asset_contract_id, amount)` again from an (independently attacker-supplied) `Asset`, and makes the *shared* `ERC20TransfersCheckingAccount` call `IERC20::transfer(beneficiary, amount)` on `asset_contract_id`: [3](#0-2) 

Crucially, the deposit half never verifies that:
- `asset_contract_id`/`amount` match what the *same* XCM program's `withdraw_asset` step actually moved into the checking account, or
- the checking account's balance of that specific ERC20 is earmarked for this particular caller.

It simply issues `transfer(beneficiary, amount)` on the checking account's pooled balance of whatever ERC20 contract the caller names via the `AccountKey20` junction. This is the same missing-invariant pattern as `exerciseInternal`'s `IERC20(tapSendData.tapOftAddress).safeTransfer(from, tapAmount)` — an attacker-chosen token address, plus an attacker-chosen amount, transferred out of a shared balance to an attacker-chosen beneficiary, without confirming the funds were actually earned/deposited by that same request. The `ERC20TransfersCheckingAccount` is a single well-known `PalletId`-derived account holding pooled balances of *every* ERC20 routed through XCM for *all* users: [4](#0-3) 

Because `Matcher` accepts any `AccountKey20` address as a valid "asset id" with no registry/allow-list check, and the transactor is reachable through `pallet_xcm::execute` (any signed account can submit a local XCM program) as one of the configured `AssetTransactors`: [5](#0-4) 

an attacker can build a two-instruction local XCM program that withdraws a negligible/zero amount of a token they own into the checking account, then issues a `DepositAsset` naming an unrelated, valuable ERC20 contract address (any real ERC20 whose location the pooled checking account happens to be currently holding balance of, e.g. left over from another user's in-flight or partially-processed message) with a beneficiary of the attacker's choosing. Nothing in `deposit_asset_with_surplus` ties the deposited `asset_contract_id`/`amount` back to what was actually withdrawn in the same message — the checking account is a single undifferentiated pot per ERC20 contract, not partitioned per-user or per-message.

### Impact Explanation
If an attacker can get `deposit_asset_with_surplus` invoked for an ERC20 contract address and amount they did not themselves deposit in the same atomic XCM execution, they steal real ERC20 value held by the chain's shared checking account, directly draining other users' bridged/wrapped ERC20 balances — a direct theft-of-funds impact matching the "theft or unbacked mint or unlock" and "public underpriced work" impact categories.

### Likelihood Explanation
The entry point (`pallet_xcm::execute`/general local XCM execution reaching `AssetTransactors`) is available to any unprivileged, signed account — no malicious relayer, validator, governance action, or leaked key is required. The exploit only requires constructing a two-step withdraw/deposit XCM naming two different `AccountKey20` asset locations, which is fully within an ordinary user's control given `ERC20Matcher` accepts arbitrary contract addresses. Full exploitability further depends on being able to reliably get non-zero balance sitting in the shared checking account for a targeted contract at the moment of the attack (e.g. via interleaved/partial multi-asset XCM programs, refunds, or trapped-asset paths that leave residual ERC20 balance in the checking account) — this residual-balance precondition was not fully traced end-to-end in the available code/tests, so likelihood should be treated as **plausible but not fully confirmed** without further runtime-level tracing of all code paths that can leave ERC20 balance parked in `ERC20TransfersCheckingAccount` between messages.

### Recommendation
- Bind the deposit half of the transactor to the specific withdraw that funded it (e.g., process withdraw+deposit for ERC20 assets as a single atomic transfer instruction rather than two independently resolved legs through a shared pooled account), or
- Maintain a per-asset, per-in-flight-message ledger of what was actually credited to the checking account before allowing `deposit_asset_with_surplus` to debit it, and reject any deposit whose `(asset_contract_id, amount)` was not matched by a corresponding withdraw in the same XCM execution context.

### Proof of Concept
Conceptually mirrors the TOFT PoC:
1. Attacker submits a local XCM (via `pallet_xcm::execute`) containing:
   - `WithdrawAsset` for a trivial/self-owned ERC20 (`AccountKey20{key: attacker_owned_token}`, amount≈0/small) — moves a negligible amount into `ERC20TransfersCheckingAccount`. [1](#0-0) 
   - `DepositAsset` naming `AccountKey20{key: victim_erc20_contract}` with `amount` set to the checking account's actual balance of that contract, beneficiary = attacker's own address.
2. `deposit_asset_with_surplus` performs `IERC20(victim_erc20_contract).transfer(attacker, amount)` from `ERC20TransfersCheckingAccount`, with no check that this amount/token corresponds to what the attacker withdrew. [3](#0-2) 
3. Result: attacker receives ERC20 tokens they never deposited, drained from the shared pool.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-169)
```rust
	fn withdraw_asset_with_surplus(
		what: &Asset,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(AssetsInHolding, Weight), XcmError> {
		tracing::trace!(
			target: "xcm::transactor::erc20::withdraw",
			?what, ?who,
		);
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-266)
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
```

**File:** prdoc/stable2506/pr_7762.prdoc (L9-14)
```text
      This PR introduces an Asset Transactor for dealing with ERC20 tokens and adds it to Asset Hub
      Westend.
      This means asset ids of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` will be
      matched by this transactor and the corresponding `transfer` function will be called in the
      smart contract whose address is `key`.
      If your chain uses `pallet-revive`, you can support ERC20s as well by adding the transactor, which lives
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
