### Title
ERC20 XCM asset transactor credits `AssetsInHolding` based on an attacker-chosen, unregistered contract's self-reported return value — ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` derive the target Solidity contract address directly from the `AccountKey20` field of the XCM `Asset` location supplied in the message being executed, then call `transfer()` on that address and treat a `true` boolean return as proof that real value moved. There is no check anywhere in this path that the address is a genuine, protocol-registered ERC20 contract (unlike `pallet-assets`, where every asset id must exist in the `Asset` storage map before any approval/transfer logic runs). This is the same broken invariant as the Magnetar bug: a component that is trusted to move/account for value calls into a caller-supplied contract address without verifying it against a registry, and blindly trusts that contract's response.

### Finding Description
`ERC20Transactor::withdraw_asset_with_surplus` resolves `(asset_id, amount)` via `Matcher::matches_fungibles(what)` [1](#0-0) , and per the PR documentation, any XCM asset id of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` is matched, with the `key` field used verbatim as the smart contract address to invoke — there is no assertion that `key` corresponds to a token the runtime actually recognizes [2](#0-1) .

It then issues a `bare_call` of `IERC20::transferCall` against that attacker-chosen address, and on success treats the boolean ABI return as ground truth, minting an `Erc20Credit(amount)` directly into `AssetsInHolding` for whatever `amount` was declared in the (also attacker-controlled) XCM `Asset` value: [3](#0-2) . Nothing forces the invoked contract to actually debit any real balance from `who` — a trivial fake contract can implement `transfer()` to unconditionally `return true` without touching any storage.

The corresponding `deposit_asset_with_surplus` path performs the mirror operation, calling `transfer()` on the same attacker-controlled contract from the fixed `TransfersCheckingAccount`, again trusting the boolean return with no validation that the checking account ever held a genuine balance of that "token" [4](#0-3) .

Compare this to `pallet-assets`, where `do_transfer_approved` and friends only operate on assets that are first registered via `Asset::<T, I>::get(&id)` and validated (`AssetStatus::Live`) before any accounting/approval logic executes [5](#0-4) . `ERC20Transactor` has no analogous registration gate — the "market" (ERC20 contract) is exactly as unvalidated as the BigBang/Singularity addresses in the Magnetar report, and the trust granted (blindly accepting the contract's own claim about the transfer's success) plays the same role as the unconditional YieldBox allowance granted to the unchecked BigBang address in the original bug.

This transactor is wired into Asset Hub Westend's live `AssetTransactors` tuple used by the XCM executor: [6](#0-5) .

### Impact Explanation
`AssetsInHolding` is the XCM executor's internal ledger of "real" value available for the remainder of an XCM program (further transfers, `ExchangeAsset`/swap through the trusted asset-conversion pools, fee payment, deposit to arbitrary beneficiaries, etc.). Because the credit into holding is backed only by an unauthenticated boolean from an attacker-deployed contract rather than any verified balance movement, an attacker can manufacture an arbitrary amount of "backed" holding value from a location whose `AccountKey20` points at their own contract. If that holding is subsequently exchanged for genuine assets (e.g., via asset-conversion pools or deposited to accounts that treat holding contents as settled value), this constitutes an unbacked-mint / theft primitive with direct chain-fund-loss impact, matching the "theft or unbacked mint" impact category.

### Likelihood Explanation
The path is reachable by any account able to get an XCM program executed with itself as origin (e.g., via `pallet_xcm::execute`/`Transact`), requiring only deployment of a trivial fake ERC20 contract via `pallet-revive` and crafting an `Asset` location whose `AccountKey20` key is that contract's address — no privileged role, relayer, validator, or governance action is needed, matching an unprivileged public-entrypoint attacker profile.

### Recommendation
Before trusting a `transfer()` return value from a contract resolved via `Matcher::matches_fungibles`, verify the resolved `H160` address is a protocol-registered ERC20 asset (e.g., checked against a maintained registry/allow-list analogous to `pallet-assets`'s `Asset` storage map, or restricted to assets created through a controlled foreign-asset creation flow) rather than accepting any structurally-matching `AccountKey20` location. Additionally, verify actual balance changes (e.g., via `balanceOf` before/after) rather than relying solely on the callee's self-reported boolean return.

### Proof of Concept
1. Deploy a `FakeERC20` contract via `pallet-revive` whose `transfer(address,uint256)` always returns `true` without any real balance accounting.
2. Submit (as any signed account) an XCM program via `pallet_xcm::execute` (or `Transact`) containing a `WithdrawAsset`/`TransferAsset` instruction whose `Asset.id` is `{ parents: 0, interior: X1(AccountKey20 { key: <FakeERC20 address>, network }) }` with an arbitrary large `amount`.
3. `ERC20Transactor::withdraw_asset_with_surplus` matches this location, calls `FakeERC20.transfer(checking_account, amount)`, which returns `true` with no real balance movement, and mints `AssetsInHolding` credit of `amount` into holding.
4. Chain a further instruction (e.g., `ExchangeAsset` against a real liquidity pool, or `DepositAsset` to an attacker beneficiary) that consumes this unbacked holding credit as if it were genuine value, resulting in the attacker extracting real backing assets or value for tokens that were never actually transferred.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-164)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-208)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-298)
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

**File:** prdoc/stable2506/pr_7762.prdoc (L6-19)
```text
doc:
  - audience: Runtime Dev
    description: |
      This PR introduces an Asset Transactor for dealing with ERC20 tokens and adds it to Asset Hub
      Westend.
      This means asset ids of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` will be
      matched by this transactor and the corresponding `transfer` function will be called in the
      smart contract whose address is `key`.
      If your chain uses `pallet-revive`, you can support ERC20s as well by adding the transactor, which lives
      in `assets-common`.
  - audience: Runtime User
    description: |
      This PR allows ERC20 tokens on Asset Hub to be referenced in XCM via their smart contract address.
      This is the first step towards cross-chain transferring ERC20s created on the Hub.
```

**File:** substrate/frame/assets/src/functions.rs (L1019-1023)
```rust
		let mut owner_died: Option<DeadConsequence> = None;

		let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(d.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);

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
