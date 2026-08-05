## Finding

The Sandclock report's core broken invariant is: *code assumes a token transfer moves exactly the nominal amount, and credits internal accounting with that nominal amount instead of the actually-received amount*, which breaks for fee-on-transfer/deflationary ERC20 tokens. The exact structural analog exists in Polkadot SDK's `ERC20Transactor`, the `TransactAsset` implementation that lets XCM programs move ERC20 tokens (via `pallet-revive`) on Asset Hub.

### Title
ERC20Transactor credits XCM holding with the nominal transfer amount instead of the amount actually received, causing permanent fund lock for fee-on-transfer/deflationary ERC20 tokens - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` calls the ERC20 `transfer()` function to move tokens from the user to a shared `TransfersCheckingAccount`, then unconditionally credits the XCM holding register with the full nominal `amount` requested — it never checks the checking account's actual balance delta. [1](#0-0)  Any ERC20 contract that charges a transfer fee (or is deflationary/rebasing) will cause the checking account to receive less than `amount`, while the holding register still records `amount` via `Erc20Credit(amount)`. `deposit_asset_with_surplus` later tries to move the full recorded `amount` out of the checking account to the beneficiary. [2](#0-1)  Because the ERC20 asset-id scheme is fully permissionless — any `AccountKey20` location is matched directly to a smart-contract address, with no admin allow-list — an unprivileged user can deploy their own fee-charging ERC20 contract and trigger this path, per the transactor's own PR description. [3](#0-2)  The transactor is wired directly into Asset Hub Westend's `AssetTransactors` list, so it is reachable from any `pallet_xcm::execute`/`send` XCM program. [4](#0-3) 

### Finding Description
The flow is:
1. `WithdrawAsset` instruction → `withdraw_asset_with_surplus` calls `IERC20::transferCall{to: checking_address, value: amount}` from the user's account. [5](#0-4)  If the token contract charges a fee, the checking account receives `amount - fee`, not `amount`.
2. Regardless of the real balance change, on success the holding is credited with `Erc20Credit(amount)` — the *nominal*, not received, amount. [6](#0-5) 
3. `DepositAsset` instruction → `deposit_asset_with_surplus` calls `IERC20::transferCall{to: beneficiary, value: amount}` *from the checking account*, using the same recorded `amount`. [7](#0-6)  Since the checking account's real token balance is `amount - fee` (less than `amount`), the ERC20 `transfer` reverts or returns `false`, and the transactor returns `Err`. [8](#0-7) 
4. The XCM executor's transactional/retry-pass semantics abort the `DepositAsset` instruction and trap the restored holding (still recording the same inflated `amount`) rather than delivering it. [9](#0-8) 

Because the trapped claim still references the *nominal* `amount`, and the checking account's on-chain ERC20 balance for that asset never actually held that much, any retry of the trapped deposit (e.g. via `claim_assets`) fails identically. The tokens the user genuinely transferred (`amount - fee`) remain stuck in the shared checking account with no code path that reconciles the shortfall — permanently locking user funds. This mirrors the Sandclock vault bug precisely: an internal ledger entry (`Erc20Credit`/holding) is created assuming a 1:1, fee-free transfer, with no before/after balance check like the (even flawed) `_transferAndCheckUnderlying()` had.

### Impact Explanation
This is a permanent user-fund lock: assets debited from the user's real ERC20 balance become unrecoverable because the recorded credit amount in the XCM holding/trap can never be satisfied by the checking account's actual token balance for a fee-on-transfer contract. This falls squarely within the in-scope impact "permanent user-fund ... lock" for `contracts or revive execution` and `asset accounting`, and it does not require any admin, relayer, validator, or malicious peer — only an ordinary user deploying and referencing a standard-but-fee-charging ERC20 contract.

### Likelihood Explanation
The precondition — any user being able to reference an arbitrary `AccountKey20` contract address as an XCM asset id without registration/allow-listing — is confirmed by the feature's own PR description. [3](#0-2)  Fee-on-transfer, deflationary, and rebasing ERC20 semantics are common and legal under the ERC20 standard (as the original report notes, even USDT-class tokens can add fees later), so this is not a contrived edge case; it is a systemic gap in the transactor's accounting model, present for every ERC20 asset routed through the checking account.

### Recommendation
`withdraw_asset_with_surplus` and `deposit_asset_with_surplus` must measure the checking/beneficiary account's actual ERC20 balance before and after the `transferCall`, and use the observed delta — not the nominal `amount` — when constructing/consuming `Erc20Credit`. Alternatively, explicitly reject ERC20 assets whose transfer delta does not match the requested amount (fail closed, analogous to Sandclock's own mitigation), and ensure the checking account's balance can never be over-credited relative to real holdings.

### Proof of Concept
1. Deploy a standard ERC20 contract via `pallet-revive` on Asset Hub Westend that charges e.g. a 1% fee on every `transfer()` (fully valid ERC20 behavior).
2. As any unprivileged account, hold some of this token and call `pallet_xcm::execute` with an XCM program: `WithdrawAsset((AccountKey20{key: <contract>}, amount)) -> DepositAsset(All, beneficiary)`.
3. `WithdrawAsset` succeeds: `withdraw_asset_with_surplus` moves `amount` from caller to `ERC20TransfersCheckingAccount`, but the checking account actually receives `amount * 0.99` due to the fee; the holding is nonetheless credited with the full `amount`. [10](#0-9) 
4. `DepositAsset` calls `deposit_asset_with_surplus`, which attempts to transfer the full `amount` out of the checking account; this reverts/fails because the checking account only holds `amount * 0.99`. [11](#0-10) 
5. The instruction aborts and the holding (still valued at the un-reconciled `amount`) is trapped; any later `claim_assets` retry for the same amount fails identically, leaving the user's already-debited tokens permanently stuck in the checking account.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-203)
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

**File:** prdoc/stable2506/pr_7762.prdoc (L10-19)
```text
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

**File:** polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs (L34-71)
```rust
/// A single sub-ED deposit fails, the instruction is aborted, and the leftover holding is
/// trapped by `post_process` — funds are not lost.
#[test]
fn failed_deposit_aborts_instruction_and_post_process_traps_holding() {
	add_asset(SENDER, (Here, 1u128)); // 1 < ExistentialDeposit (=2 in mock)

	let xcm = Xcm::<TestCall>::builder_unsafe()
		.withdraw_asset((Here, 1u128))
		.deposit_asset(All, RECIPIENT)
		.build();

	let (mut vm, weight) = instantiate_executor(SENDER, xcm.clone());

	// `bench_process` returns `Err` because the retry-pass deposit failure now bubbles up.
	let result = vm.bench_process(xcm);
	let err = result.expect_err("retry-pass deposit failure must bubble up");

	// Mirror what `XcmExecutor::execute` does between `process` and `post_process`: register
	// the instruction error so `post_process` produces `Outcome::Incomplete`.
	vm.set_error(Some((err.index, err.xcm_error)));

	let outcome = vm.bench_post_process(weight);
	assert!(
		matches!(outcome, Outcome::Incomplete { .. }),
		"expected Outcome::Incomplete, got {outcome:?}"
	);

	// Recipient never received anything.
	assert!(asset_list(RECIPIENT).is_empty());

	// `post_process` trapped the holding (which `transactional_process` had restored after
	// the failed `DepositAsset`). The mock `TestAssetTrap` accumulates everything under
	// `TRAPPED_ASSETS`.
	assert_eq!(
		asset_list(TRAPPED_ASSETS),
		vec![(Here, 1u128).into()],
		"undeposited assets must be trapped, not silently lost"
	);
```
