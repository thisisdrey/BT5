Audit Report

## Title
Hard-coded ERC20 transfer weight limit in `ERC20Transactor` permanently fails and locks funds for ERC20 tokens whose `transfer` exceeds the fixed gas budget - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` both invoke an ERC20 contract's `transfer` function via `pallet_revive::Pallet::<T>::bare_call` using a single, globally fixed `weight_limit = WeightLimit::get()` bound to the `ERC20TransferGasLimit` constant, with no per-asset override. [1](#0-0)  Any ERC20 whose `transfer` implementation legitimately consumes more weight than this fixed budget will unconditionally fail every deposit, and because the source-side balance is moved into `TransfersCheckingAccount` before the deposit step, and `check_in`/`check_out` are `Unimplemented`, tokens already withdrawn can become stuck there if the destination-side transfer repeatedly hits the same fixed limit. [2](#0-1) 

## Finding Description
The code exactly matches what is described: both transact functions build an `IERC20::transferCall` and execute it through `bare_call` with `TransactionLimits::WeightAndDeposit { weight_limit: WeightLimit::get(), deposit_limit: StorageDepositLimit::get() }` [1](#0-0) , and `ERC20Transactor` is wired into Asset Hub Westend's `AssetTransactors` with a fixed `ERC20TransferGasLimit` of `Weight::from_parts(500_000_000_000, 10 * 1024 * 1024)`, described in a comment as "taken from the real gas and deposits of a standard ERC20 transfer call" with no per-token override mechanism. The failure branch explicitly acknowledges the design limitation: "This error could've been duplicate smart contract, out of gas, etc. If the issue is gas, there's nothing the user can change in the XCM that will make this work since there's a hardcoded gas limit." [3](#0-2)  `check_in`/`check_out` are indeed hard-coded to `Err(XcmError::Unimplemented)`/no-op, confirming there is no teleport-based reclamation path for assets caught mid-flow. [2](#0-1) 

However, the claim's characterization of severity/likelihood requires scrutiny that the provided evidence does not fully resolve:
- The configured weight (500 billion ref-time units ≈ 500ms of reference-hardware compute time, plus 10 MiB proof size) is extremely generous compared to what a typical simple or moderately complex ERC20 `transfer` (including fee-on-transfer, rebasing, or hook-calling variants) would realistically consume in a PVM/EVM-compatibility contract execution. Whether any realistic ERC20 contract exceeds this specific fixed budget is not demonstrated in-repo; the claim's PoC is hypothetical ("Deploy ... whose `transfer` function performs additional logic ... such that its weight consumption exceeds `ERC20TransferGasLimit`") rather than a concrete, reproduced failure with a real-world token pattern.
- The "permanent lock" characterization assumes that `ClaimAsset`/trap-recovery paths always re-invoke `deposit_asset_with_surplus` with the identical fixed weight limit and that there is no other operational path (e.g., a future runtime upgrade to `ERC20TransferGasLimit`, or governance-driven remediation) to recover a stuck balance in `TransfersCheckingAccount`. The repository code does confirm no automatic per-asset override exists at present, but a `ERC20TransferGasLimit` runtime constant can in principle be adjusted via a runtime upgrade — this is a normal, non-privileged-abuse remediation path for a misconfigured constant, not evidence that funds are unrecoverable by any means whatsoever.

## Impact Explanation
If real ERC20 contracts exist (or can be deployed) whose `transfer` implementation exceeds the fixed `ERC20TransferGasLimit`, this pattern does fit the "permanent user-fund or bridge-state lock" category: value already moved into `TransfersCheckingAccount` during `withdraw_asset_with_surplus` cannot be released to the intended beneficiary via `deposit_asset_with_surplus` because the exact same insufficient, non-adjustable weight budget is reused for every retry/claim attempt. [4](#0-3) [5](#0-4) 

## Likelihood Explanation
Likelihood depends entirely on whether an ERC20 contract that legitimately needs more than 500,000,000,000 ref-time units (roughly 500ms of reference-hardware execution) and 10 MiB of proof size for a single `transfer` call can exist and be registered as an XCM-transactable asset via `ERC20Matcher`. The repository provides no test, benchmark, or documented precedent demonstrating that a realistic ERC20 `transfer` implementation exceeds this specific, quite generous budget — the referenced Snowbridge `ConstantGasMeter`/LDO incident is an external analogy, not evidence located in this repository's revive-based `ERC20Transactor`. Without such a demonstration, the likelihood of triggering this exact failure with a legitimate, non-adversarial token cannot be confirmed as high from the code alone.

## Recommendation
Even though the "unconditionally unrecoverable" characterization is not fully substantiated, the design gap is real and should be hardened: support a per-asset configurable weight/gas budget at registration time (rather than one global constant), and/or perform a dry-run weight estimation before committing `withdraw_asset_with_surplus`, so that failures are detected before funds are moved into `TransfersCheckingAccount`. Additionally, provide an explicit, well-tested recovery/exit path for balances already resident in `TransfersCheckingAccount` (e.g., an admin-gated but auditable extrinsic, or automatic weight escalation on retry) independent of runtime-constant upgrades.

## Proof of Concept
The claim's PoC steps are plausible given the code but rely on an unverified precondition (existence of an ERC20 contract whose `transfer` exceeds `ERC20TransferGasLimit`): register such a contract via `ERC20Matcher`, execute a `WithdrawAsset`/`DepositAsset` XCM through `ERC20Transactor`, and observe `deposit_asset_with_surplus` returning `XcmError::FailedToTransactAsset("ERC20 contract execution errored")` on every retry due to the fixed `weight_limit`. This has not been demonstrated in-repo with a concrete failing contract; it is a design-level risk analysis rather than a reproduced failure.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L132-149)
```rust
	fn can_check_in(_origin: &Location, _what: &Asset, _context: &XcmContext) -> XcmResult {
		// We don't support teleports.
		Err(XcmError::Unimplemented)
	}

	fn check_in(_origin: &Location, _what: &Asset, _context: &XcmContext) {
		// We don't support teleports.
	}

	fn can_check_out(_destination: &Location, _what: &Asset, _context: &XcmContext) -> XcmResult {
		// We don't support teleports.
		Err(XcmError::Unimplemented)
	}

	fn check_out(_destination: &Location, _what: &Asset, _context: &XcmContext) {
		// We don't support teleports.
	}

```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-216)
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
