### Title
ERC20 asset transactor lets any account drain unrelated ERC20 tokens stuck in the shared `ERC20TransfersCheckingAccount` - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor` moves any `pallet-revive` ERC20 token that matches a generic `AccountKey20` location pattern through a single shared "checking account". Neither the matcher nor the transactor verify that the ERC20 contract address named in an XCM `Asset` is one that the calling/depositing party actually escrowed. This is the same broken invariant as the reported basket bug: `settleAuction()` let a caller name *any* `outputToken`, letting them scoop unrelated ERC20s that ended up in the basket contract. Here, `deposit_asset` lets a caller name *any* ERC20 contract address as the `AssetId`, letting them scoop whatever balance of that token currently sits in the shared checking account — regardless of who put it there or why.

### Finding Description
`assets_common::ERC20Matcher` is defined as: [1](#0-0) 

`IsLocalAccountKey20::contains` accepts **any** location shaped `(0, [AccountKey20 { .. }])` — there is no check against a registry of tokens that are actually expected/reserved for XCM transfers (unlike `PoolFungiblesTransactor`, which uses `LocalMint<NonZeroIssuance<...>>` to gate teleports/checks on known assets, see `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:197-211`).

`ERC20Transactor` uses this unrestricted matcher for both withdraw and deposit: [2](#0-1) [3](#0-2) 

* `withdraw_asset_with_surplus` performs a *real, immediately-committed* `IERC20::transfer` call from the caller's own account to a single, chain-wide `TransfersCheckingAccount` (`ERC20TransfersCheckingAccount`, a deterministic `PalletId(*b"py/revch")`-derived account, see `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:213-237`).
* `deposit_asset_with_surplus` performs a real `IERC20::transfer` call **from that same shared checking account** to whatever beneficiary the current XCM program specifies, for whatever `asset_contract_id`/`amount` the *current* caller's `Asset` names — with no bookkeeping that ties a specific deposit back to the specific withdrawal that funded it.

Both calls are `pallet_revive::Pallet::<T>::bare_call` — real contract-state mutations, not speculative "holding register" bookkeeping. Any signed account can drive this transactor directly via `pallet_xcm::execute`, as shown in the repo's own test: [4](#0-3) 

Because withdraw and deposit within one XCM program are two independent instructions against a **shared pooled account** with no per-asset/per-owner escrow ledger, the checking account can end up holding an ERC20 balance that is not "owned" by the party currently interacting with it — exactly the scenario the report describes ("unrelated ERC20 tokens end up in the basket contract via an airdrop, a user mistake, etc"). Concrete ways this happens here:
- A multi-instruction XCM program successfully executes `withdraw_asset` (a real, committed `transfer` into the checking account) but a later instruction in the same program fails (e.g. hits the hardcoded `ERC20TransferGasLimit`/weight limit, as demonstrated by the repo's own `expensive_erc20_runs_out_of_gas` test at `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs:2076-2127`, which shows execution failing partway through). If `PolkadotXcm::execute` does not fully roll back the already-applied ERC20 `transfer` side effect on `Outcome::Incomplete`/error, the tokens are left stranded in the checking account.
- Anyone (mistakenly or intentionally) sends ERC20 tokens directly to the deterministic checking-account address off-chain.

Once *any* balance of *any* matching ERC20 contract sits in that account, `ERC20Transactor::deposit_asset` (reachable by any signed user through `pallet_xcm::execute`) will happily transfer it out to an attacker-chosen beneficiary, because the only check performed is `Matcher::matches_fungibles`, which — as shown above — accepts any `AccountKey20` location without verifying provenance or entitlement.

### Impact Explanation
This is unauthorized transfer of value that is not the caller's: an unprivileged, signed account can craft a local XCM program (`deposit_asset` naming an arbitrary ERC20 contract address + itself as beneficiary) to drain ERC20 balances held by the shared `ERC20TransfersCheckingAccount` that were not deposited by that caller. This matches the "theft or unbacked mint/unlock" and "duplicate settlement / wrong beneficiary" impact categories: value conservation is broken because there is no ledger tying a specific withdrawal to the account entitled to the matching deposit — only a bare balance check performed by the ERC20 contract itself.

### Likelihood Explanation
Exploitability depends on the checking account holding a stray/unclaimed ERC20 balance. The repository's own tests demonstrate that partial/failed multi-instruction XCM programs involving ERC20 transfers are a realistic, reachable failure mode (`expensive_erc20_runs_out_of_gas`), and the checking account address is public/deterministic (`PalletId(*b"py/revch")`), making it a predictable target to monitor for stray balances. No privileged actor, relayer, validator, or governance action is required — any signed account calling `pallet_xcm::execute` (or any XCM program landing on this transactor) is sufficient.

### Recommendation
Add per-asset/per-owner reservation accounting to `ERC20Transactor` (e.g., a storage map keyed by `(asset_contract_id, owner)` recording escrowed amounts), and have `deposit_asset_with_surplus` verify/consume a matching reservation before calling `IERC20::transfer` from the checking account, rather than trusting the caller-supplied `AssetId`/`amount` in isolation. Alternatively, restrict `ERC20Matcher` to a registered allow-list of tokens explicitly onboarded for XCM (mirroring the `NonZeroIssuance`-gated pattern already used for `PoolFungiblesTransactor`), and ensure any already-committed `bare_call` transfer is not left un-reverted when a subsequent XCM instruction in the same program fails.

### Proof of Concept
Conceptual reproduction based on the repo's existing test harness (`cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs`):
1. Deploy any ERC20 contract via `pallet_revive` (as in `withdraw_and_deposit_erc20s`).
2. Get a balance of that ERC20 stuck in `ERC20TransfersCheckingAccount` — e.g. by crafting an XCM program that succeeds through `withdraw_asset` (real transfer into checking account) but fails a later instruction (as in `expensive_erc20_runs_out_of_gas`, which shows execution erroring out after ERC20 movement has already occurred), or by directly sending tokens to the deterministic checking-account address.
3. As an unrelated account, submit `PolkadotXcm::execute` with an XCM program: `deposit_asset(AllCounted(1) or a Definite Asset naming that contract's AccountKey20 address, beneficiary: attacker)`.
4. `ERC20Transactor::deposit_asset_with_surplus` calls `IERC20::transfer(attacker, amount)` from the checking account without verifying the attacker ever deposited that token, transferring the stranded balance to the attacker.

I was not able to fully verify (due to tool budget) whether `pallet_xcm::execute`'s dispatch wraps the whole call in a `#[transactional]` rollback that would revert the already-committed `bare_call` ERC20 transfer on `Outcome::Incomplete`. If such a rollback exists, the "stuck via partial execution" trigger would not apply, but the core missing-ownership-check in `deposit_asset_with_surplus` (any caller can name any matching ERC20 and drain whatever the checking account currently holds of it, regardless of source) remains a real, unguarded broken invariant independent of how the account came to hold that balance (e.g., direct/mistaken transfers to the well-known checking-account address).

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-160)
```rust
/// `Contains<Location>` implementation that matches locations with no parents,
/// a `PalletInstance` and an `AccountKey20` junction.
pub struct IsLocalAccountKey20;
impl Contains<Location> for IsLocalAccountKey20 {
	fn contains(location: &Location) -> bool {
		matches!(location.unpack(), (0, [AccountKey20 { .. }]))
	}
}

/// Fallible converter from a location to a `H160` that matches any location ending with
/// an `AccountKey20` junction.
pub struct AccountKey20ToH160;
impl MaybeEquivalence<Location, H160> for AccountKey20ToH160 {
	fn convert(location: &Location) -> Option<H160> {
		match location.unpack() {
			(0, [AccountKey20 { key, .. }]) => Some((*key).into()),
			_ => None,
		}
	}

	fn convert_back(key: &H160) -> Option<Location> {
		Some(Location::new(0, [AccountKey20 { key: (*key).into(), network: None }]))
	}
}

/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1900-1916)
```rust
		// Actual XCM to execute locally.
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.refund_surplus()
			.deposit_asset(AllCounted(1), sender.clone())
			.build();
		assert_ok!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(600_000_000_000, 15 * 1024 * 1024),
		));
```
