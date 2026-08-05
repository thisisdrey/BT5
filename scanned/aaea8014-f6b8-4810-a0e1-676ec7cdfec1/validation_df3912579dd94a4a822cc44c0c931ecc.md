### Title
`ERC20Transactor::deposit_asset_with_surplus` blindly calls `bare_call` on the matched asset's address, trapping cross-chain funds when the matched location does not resolve to a deployed ERC20 contract - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` is an XCM `TransactAsset` implementation used to move ERC20 tokens held under `pallet-revive` contracts during XCM asset settlement. Both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` unconditionally derive an `H160` from `Matcher::matches_fungibles(what)` and treat it as a live ERC20 contract address, invoking `pallet_revive::Pallet::<T>::bare_call` with a Solidity `transfer` call. There is no check that a contract actually exists at that address before making the call, mirroring exactly the reported bug class: assuming a token always has a callable contract when it may not.

### Finding Description
`withdraw_asset_with_surplus` and `deposit_asset_with_surplus` ( [1](#0-0) , [2](#0-1) ) both take an `asset_id`/`asset_contract_id: H160` produced by `Matcher::matches_fungibles(what)` and immediately call:

```rust
pallet_revive::Pallet::<T>::bare_call(..., asset_id, ..., data, ...)
```

where `data` is an ABI-encoded `IERC20::transferCall`. This mirrors the external report's pattern verbatim: the code assumes `IERC20`/ERC20 semantics exist at the resolved address, with no verification that a contract implementing `IERC20` is actually deployed there. If `bare_call` targets an address with no contract code (or a contract that doesn't implement the interface), execution errors out and the code paths only handle "did_revert" / decode failure / generic execution error, all of which resolve to `XcmError::FailedToTransactAsset(...)` ( [3](#0-2)  and [4](#0-3) ).

Any `Matcher: MatchesFungibles<H160, u128>` configuration that can map a `Location` to an `H160` that is not backed by an actually-deployed ERC20 contract (e.g. a location representing a "native"/non-contract-backed asset, a mis-derived address, or an asset whose contract has not yet been instantiated on this chain) will cause:
- `withdraw_asset_with_surplus` to fail safely (transaction aborts, no state change) — not exploitable on its own.
- `deposit_asset_with_surplus` to fail *after* the corresponding assets have already been taken out of holding elsewhere in a multi-leg XCM program (e.g. after a successful `WithdrawAsset`/reserve-transfer on the source side, or after other legs of the same XCM already executed). Since `TransactAsset::deposit_asset` failures in the XCM executor result in the assets being trapped (`AssetsTrapped`) rather than delivered to the intended beneficiary, this reproduces the report's core defect: a legitimate cross-chain deposit that should succeed instead fails because the code assumed a contract exists where none does — the difference being that here the outcome is fund lock/trap on the destination chain rather than a plain revert.

Existing guards do not prevent this: there is no `Ext::code_size`/existence check, no fallback path for "asset without a deployed contract," and no distinction between "this asset never has EVM bytecode" versus "temporary revert." The `Matcher` trait is generic and configuration-dependent, so correctness depends entirely on runtime wiring guaranteeing that every location it can match is backed by contract code — an invariant that is not enforced by the transactor itself.

### Impact Explanation
This is a public-entrypoint path (any user submitting/triggering an XCM program that routes an ERC20-classified asset through this transactor) that can cause a legitimate deposit to be trapped rather than delivered to the rightful beneficiary — a violation of the "settle exactly once to the rightful beneficiary and amount" invariant and a form of permanent user-fund lock, consistent with the accepted impact categories. It does not require a malicious peer, validator, relayer, or governance actor — only a matcher configuration or asset state where the resolved `H160` is not (yet) contract-bearing.

### Likelihood Explanation
Likelihood is Medium: it requires either (a) a `Matcher` configuration that legitimately maps some location to a non-contract address (e.g., due to misconfiguration, or a location for an asset whose ERC20 contract hasn't been instantiated/migrated yet), or (b) a race where the target contract is destroyed/not-yet-deployed between matching and settlement. This is analogous to the original report's medium-likelihood classification (native-token DAO deployments happen but aren't the default case).

### Recommendation
Before issuing the `bare_call` in both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, verify that the resolved `asset_id`/`asset_contract_id` corresponds to a contract with deployed code (e.g., via `pallet_revive`'s account-info/code-existence lookup) and return a distinct, clearly-typed `XcmError` (rather than a generic `FailedToTransactAsset`) when no contract exists. Additionally, ensure the `Matcher` implementations used with `ERC20Transactor` can never resolve to an address lacking contract code, or add a compile/runtime-time invariant check for this precondition.

### Proof of Concept
Conceptual PoC (cannot be executed without live filesystem/terminal access, so this describes the reproduction steps for a background agent):
1. Configure a test runtime with `ERC20Transactor` wired to a `Matcher` that matches a `Location` (e.g., representing the chain's native token or an un-instantiated foreign asset) to an `H160` address that has no deployed contract code in `pallet-revive`.
2. Construct an XCM program with two legs: a `WithdrawAsset`/reserve-transfer that removes funds from a source account's holding, followed by a `DepositAsset` that resolves to the above uninstantiated `H160` via the `Matcher`.
3. Execute the XCM program and observe: `deposit_asset_with_surplus` calls `bare_call` on the address, which errors (no contract code) or reverts; the function returns `Err((what, XcmError::FailedToTransactAsset(...)))`.
4. Confirm that the XCM executor reports `AssetsTrapped` for `what` and that the intended beneficiary never receives the asset, while the source-side withdrawal already succeeded — demonstrating fund lock analogous to the reported `balanceOf()`-on-a-nonexistent-contract failure mode. [5](#0-4) [6](#0-5)

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-215)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-306)
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
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
		}
	}
```
