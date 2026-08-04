### Title
`ERC20Transactor` trusts an attacker-controlled ERC20 `transfer()` boolean return value instead of verifying real balance movement, allowing fake-collateral minting via XCM asset withdraw/deposit - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The Rubicon `withdrawForETH` bug is a case of trusting an externally-supplied, attacker-controlled contract's interface responses (`balanceOf`, `transferFrom`) to determine how much value to release, instead of confirming the contract's own balance actually changed. The same broken invariant exists in `ERC20Transactor::withdraw_asset_with_surplus` / `deposit_asset_with_surplus` in `assets-common`, which is wired into Asset Hub Westend's XCM `AssetTransactors`.

### Finding Description
`ERC20Transactor` is a `TransactAsset` implementation used by the XCM executor whenever an `Asset` id resolves to an `AccountKey20` location (i.e., any Ethereum-style contract address), per `IsLocalAccountKey20` matching referenced in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/weights/xcm/mod.rs:73` and wired in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:222-245`.

Its withdraw logic: [1](#0-0) 

calls the arbitrary ERC20 contract's `transfer(checking_address, amount)` and, if the call does not revert, **decodes only the boolean return value** and — if `true` — credits `AssetsInHolding` with `amount` of the fungible asset via `AssetsInHolding::new_from_fungible_credit`. There is no check that the `TransfersCheckingAccount`'s balance in that contract actually increased by `amount` before crediting the XCM holding register.

The deposit path mirrors this: [2](#0-1) 

Here `transfer(beneficiary, amount)` is called from the checking account, and again only the boolean return is trusted to conclude the beneficiary actually received `amount`.

Since **any unprivileged account can deploy an arbitrary Solidity contract via `pallet-revive`** (permissionless `instantiate`/`bare_instantiate`), and any `AccountKey20` address is XCM-addressable as an asset id (see the `withdraw_and_deposit_erc20s` and `smart_contract_does_not_return_bool_fails` tests in `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs:1864-2074`, which already probe partially-broken ERC20 behavior), an attacker can deploy a "fake ERC20" whose `transfer()` function:
- Always returns `true` regardless of any real balance check, and
- Freely credits `balanceOf[to] += value` without debiting a real balance (i.e., an unconstrained mint), or performs no bookkeeping at all.

The XCM `ERC20Transactor` will accept this contract's `transfer()` success at face value on both withdraw and deposit legs, exactly mirroring the Rubicon flaw where `targetPool.balanceOf()`/`transferFrom()` were trusted without confirming the contract's actual WETH balance changed.

### Impact Explanation
Because the transactor never validates that the checking account's or beneficiary's actual on-chain balance in the target contract changed by `amount`, an attacker-authored ERC20 contract can be XCM-recognized as a fungible asset whose `AssetsInHolding` credit is entirely fabricated and disconnected from any conserved balance. This "fake but XCM-valid" asset can then be:
- Deposited to arbitrary beneficiaries with a self-asserted `amount` that was never actually escrowed in the checking account, and
- Used as a counter-asset in `pallet-asset-conversion` liquidity pools or as XCM fee payment against genuine, backed assets (WND, other trust-backed assets), letting the attacker extract real value in exchange for value that was never conserved.

This matches the "theft or unbacked mint" and "public underpriced work" categories: an unprivileged, non-relayer, non-validator attacker can mint effectively unbacked collateral recognized by the chain's XCM asset accounting.

### Likelihood Explanation
Likelihood is high for the precondition (deploying a malicious contract and referencing it via XCM `withdraw_asset`/`deposit_asset` is fully permissionless and requires no governance, relayer, or validator collusion — matching the required "unprivileged attacker" threat model). The remaining step (finding a downstream consumer, e.g., a liquidity pool or fee-paying context, that treats the credited `AssetsInHolding` amount as real value) is straightforward given `pallet-asset-conversion` and XCM fee-payment paths already accept arbitrary sufficient/registered fungible locations.

### Recommendation
Before crediting `AssetsInHolding` on withdraw, or concluding success on deposit, verify the actual on-chain balance change of the `TransfersCheckingAccount` (and beneficiary) in the ERC20 contract via `balanceOf` calls taken before and after the `transfer` call, rejecting the operation (returning an `XcmError`) if the observed delta does not equal `amount`. This mirrors the original report's recommended mitigation of checking the contract's balance before and after rather than trusting return values alone.

### Proof of Concept
1. Attacker calls `pallet_revive::Pallet::instantiate` (permissionless) to deploy `FakeERC20`, whose `transfer(address to, uint256 value)` implementation unconditionally does `balanceOf[to] += value; return true;` without any sender-side debit or balance check.
2. Attacker submits an XCM program via `pallet_xcm::execute`:
   - `WithdrawAsset((AccountKey20 { key: fake_erc20_address }, amount))` — triggers `ERC20Transactor::withdraw_asset_with_surplus`, which calls `FakeERC20.transfer(checking_address, amount)`. The contract returns `true` without the checking account's real balance increasing.
   - `DepositAsset(..., beneficiary)` — triggers `deposit_asset_with_surplus`, calling `FakeERC20.transfer(beneficiary, amount)` from the checking account, again returning `true` and crediting `beneficiary`'s `balanceOf` in the fake contract.
3. The beneficiary now holds `amount` of `FakeERC20` tokens recognized by the chain's XCM asset accounting, despite no genuine value having been escrowed at any point — confirmed by inspecting `<Revive as fungibles::Inspect<_>>::balance(fake_erc20_address, &beneficiary)`, following the same assertion pattern used in `withdraw_and_deposit_erc20s` (`cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs:1925-1927`), but with the underlying contract lying about balance movement.
4. Attacker pairs `FakeERC20` in an `AssetConversion` pool against real WND, adds "liquidity" using the freely-minted fake tokens, and swaps out real WND — extracting real backed value in exchange for tokens that were never actually transferred/conserved, exactly analogous to `withdrawForETH` draining real WETH backed by a spoofed `targetPool`. [3](#0-2) [4](#0-3)

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-216)
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
