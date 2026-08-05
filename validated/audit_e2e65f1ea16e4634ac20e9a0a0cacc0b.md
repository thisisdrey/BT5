Audit Report

## Title
Non-standard (non-bool-returning) ERC20 tokens are permanently locked in the checking account by `ERC20Transactor` - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` both require the target contract's `transfer` call to return an ABI-encoded `bool` via `IERC20::transferCall::abi_decode_returns_validate`, failing with `FailedToTransactAsset("ERC20 contract result couldn't decode")` for any real, balance-moving but non-bool-returning ERC20 [1](#0-0) [2](#0-1) . Because `ERC20Transactor` never overrides `mint_asset`, it defaults to `Err(XcmError::Unimplemented)` [3](#0-2) , and this bubbles up as `AssetNotFound` in the tuple `AssetTransactors` composition since no other transactor handles ERC20 (`AccountKey20`) asset ids [4](#0-3) [5](#0-4) . This means the standard trap/`ClaimAsset` recovery path can never reconstitute a trapped `Erc20Credit`, leaving real ERC20 balance permanently stuck in `TransfersCheckingAccount` when a `WithdrawAsset` for such a token commits but a subsequent `DepositAsset` (or any later instruction) in the same program fails.

## Finding Description
`withdraw_asset_with_surplus` moves real tokens into `TransfersCheckingAccount` via `pallet_revive::bare_call` before requiring the return data decode as `bool` [6](#0-5) ; `deposit_asset_with_surplus` has the identical requirement on the return leg [7](#0-6) . In the XCM executor, `WithdrawAsset` and `DepositAsset` are each processed in their own, separate transactional scope [8](#0-7) [9](#0-8) , so a committed `WithdrawAsset` is not automatically undone by a later failing `DepositAsset`.

Critically, when an XCM program is processed via `pallet_message_queue` (UMP/DMP/XCMP messages, as opposed to the atomic `pallet_xcm::execute` extrinsic), `process_message_payload` wraps the whole message processing in `storage::with_transaction`, but only rolls back on `Err(_)` — an `Outcome::Incomplete` result (partial execution, e.g. `WithdrawAsset` succeeded then `DepositAsset` failed) is mapped to `Ok(false)` by `ProcessXcmMessage::process_message` [10](#0-9) , which is treated as `Commit`, not `Rollback` [11](#0-10) . This is an intentional design documented in `prdoc/stable2412/pr_5198.prdoc`: "Storage changes that were done while processing a message will now be rolled back when the processing returns an error. `Ok(false)` will not revert, only `Err(_)`." Consequently, the real ERC20 transfer into `TransfersCheckingAccount` from the successful `WithdrawAsset` is permanently committed even though the overall program is `Incomplete`, while the leftover synthetic `Erc20Credit` in holding is trapped via `Config::AssetTrap::drop_assets` [12](#0-11) .

Recovery via `pallet_xcm::claim_assets` builds `[ClaimAsset, DepositAsset]` [13](#0-12) , but `ClaimAsset` invokes `AssetTransactor::mint_asset`, which is unimplemented for `ERC20Transactor` and resolves to `AssetNotFound` across the tuple, so the claim extrinsic fails outright before ever reaching the `DepositAsset` leg (which would fail identically anyway for a non-bool-returning contract). No other code path in the repository is capable of moving the asset back out of the checking account for this specific asset id.

## Impact Explanation
This is a permanent, unrecoverable lock of real user funds (ERC20 balance) in `ERC20TransfersCheckingAccount`, matching the required "permanent user-fund lock" impact. It requires no privileged actor, governance, or compromised infrastructure — an ordinary holder of a non-compliant (non-bool-returning) ERC20 contract matched by `ERC20Matcher` triggers it simply by having their multi-instruction XCM program routed through the asynchronous message-queue path where a `WithdrawAsset` commits ahead of a later failing instruction.

## Likelihood Explanation
The trigger conditions are narrow but realistic: (1) the token must be a real, deployed non-standard ERC20 (returns no/invalid bool from `transfer`) matched by `ERC20Matcher`; (2) the XCM program must be processed via the message queue (UMP/DMP/XCMP) rather than the atomic `pallet_xcm::execute` extrinsic, since the extrinsic path is provably fully rolled back on any instruction failure (demonstrated by `incomplete_execute_reverts_side_effects` in `polkadot/xcm/pallet-xcm/src/tests/mod.rs`); and (3) the program must contain a `WithdrawAsset` for the non-compliant token followed by a later instruction that fails. This is achievable by any user constructing or receiving such a program, e.g., via cross-chain reserve transfers or `InitiateTransfer`-style flows that land in the local `MessageQueue`, with no privileged capability required, and is repeatable for any deployed non-compliant ERC20 contract.

## Recommendation
- Implement `mint_asset` for `ERC20Transactor` so trapped `Erc20Credit` can be reconstructed by `ClaimAsset`.
- Relax the strict `abi_decode_returns_validate`-as-bool requirement in `deposit_asset_with_surplus`/`withdraw_asset_with_surplus` to tolerate empty return data as success (mirroring `SafeERC20`-style handling), while still rejecting explicit `false` or reverts.
- Add a governance-gated emergency drain mechanism for `TransfersCheckingAccount`, or gate registration of ERC20 contracts behind a standards-conformance probe before they can be matched by `ERC20Matcher`.

## Proof of Concept
1. Deploy a `pallet_revive` ERC20-like contract (e.g., `MyTokenFake` as used in `smart_contract_does_not_return_bool_fails`) whose `transfer()` moves balances but returns a non-bool value, and have `ERC20Matcher` match its asset id.
2. Construct an XCM program with `WithdrawAsset` for this token followed by an instruction that fails after the withdraw commits (e.g., a subsequent `DepositAsset` with a decode failure, or any later failing instruction), and route it through `pallet_message_queue` (not `pallet_xcm::execute`) so `Outcome::Incomplete` maps to `Ok(false)` and is committed per `process_message_payload`.
3. Observe the real ERC20 balance move into `ERC20TransfersCheckingAccount` (committed) while the message ends `Incomplete` and the synthetic credit is trapped via `AssetsTrapped`.
4. Call `pallet_xcm::claim_assets` for the trapped asset; observe it fails immediately because `ERC20Transactor::mint_asset` is unimplemented, confirming the fund is permanently unrecoverable.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-216)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-306)
```rust
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

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L219-221)
```rust
	fn mint_asset(_what: &Asset, _context: &XcmContext) -> Result<AssetsInHolding, XcmError> {
		Err(XcmError::Unimplemented)
	}
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L408-422)
```rust
	fn mint_asset(what: &Asset, context: &XcmContext) -> Result<AssetsInHolding, XcmError> {
		for_tuples!( #(
			match Tuple::mint_asset(what, context) {
				Err(XcmError::AssetNotFound) | Err(XcmError::Unimplemented) => (),
				r => return r,
			}
		)* );
		tracing::trace!(
			target: "xcm::TransactAsset::mint_asset",
			?what,
			?context,
			"no match. did not mint asset",
		);
		Err(XcmError::AssetNotFound)
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

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L946-970)
```rust
		match instr {
			WithdrawAsset(assets) => {
				self.ensure_can_subsume_assets(assets.len())?;
				Config::TransactionalProcessor::process(|| {
					let origin = self.origin_ref().ok_or(XcmError::BadOrigin)?;
					let mut total_surplus = Weight::zero();
					let mut withdrawn = AssetsInHolding::new();
					// Take `assets` from the origin account (on-chain)...
					for asset in assets.inner() {
						let (credit, surplus) = Config::AssetTransactor::withdraw_asset_with_surplus(
							asset,
							origin,
							Some(&self.context),
						)?;
						withdrawn.subsume_assets(credit);
						// If we have some surplus, aggregate it.
						total_surplus.saturating_accrue(surplus);
					}
					// ...and place into holding.
					self.holding.subsume_assets(withdrawn);
					// Credit the total surplus.
					self.total_surplus.saturating_accrue(total_surplus);
					Ok(())
				})
			},
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1191-1202)
```rust
			DepositAsset { assets, beneficiary } => {
				self.transactional_process(|self_ref| {
					let deposited = self_ref.holding.saturating_take(assets);
					let surplus = Self::deposit_assets_with_retry(
						deposited,
						&beneficiary,
						Some(&self_ref.context),
					)?;
					self_ref.total_surplus.saturating_accrue(surplus);
					Ok(())
				})
			},
```

**File:** polkadot/xcm/xcm-builder/src/process_xcm_message.rs (L91-109)
```rust
		let (consumed, result) = match XcmExecutor::execute(origin.into(), pre, id, Weight::zero())
		{
			Outcome::Complete { used } => {
				tracing::trace!(
					target: LOG_TARGET,
					"XCM message execution complete, used weight: {used}",
				);
				(used, Ok(true))
			},
			Outcome::Incomplete { used, error: InstructionError { index, error } } => {
				tracing::trace!(
					target: LOG_TARGET,
					?error,
					?index,
					?used,
					"XCM message execution incomplete",
				);
				(used, Ok(false))
			},
```

**File:** substrate/frame/message-queue/src/lib.rs (L1569-1577)
```rust
		let transaction =
			storage::with_transaction(|| -> TransactionOutcome<Result<_, DispatchError>> {
				let res =
					T::MessageProcessor::process_message(message, origin.clone(), meter, &mut id);
				match &res {
					Ok(_) => TransactionOutcome::Commit(Ok(res)),
					Err(_) => TransactionOutcome::Rollback(Ok(res)),
				}
			});
```

**File:** polkadot/xcm/xcm-executor/src/traits/drop_assets.rs (L27-30)
```rust
pub trait DropAssets {
	/// Handler for receiving dropped assets. Returns the weight consumed by this operation.
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight;
}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1551-1567)
```rust
			let ticket: Location = GeneralIndex(assets_version as u128).into();
			let mut message = Xcm(vec![
				ClaimAsset { assets, ticket },
				DepositAsset { assets: AllCounted(number_of_assets).into(), beneficiary },
			]);
			let weight = T::Weigher::weight(&mut message, Weight::MAX).map_err(|error| {
				tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?error, "Failed to calculate weight");
				Error::<T>::UnweighableMessage
			})?;
			let mut hash = message.using_encoded(sp_io::hashing::blake2_256);
			let outcome = T::XcmExecutor::prepare_and_execute(
				origin_location,
				message,
				&mut hash,
				weight,
				weight,
			);
```
