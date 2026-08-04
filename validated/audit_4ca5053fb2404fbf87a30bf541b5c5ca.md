### Title
Unwhitelisted arbitrary-contract trust in `ERC20Transactor` combined with silent multi-asset drop causes permanent fund loss on deposit - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::deposit_asset_with_surplus`/`withdraw_asset_with_surplus` (used live in `asset-hub-westend`'s `xcm_config.rs`) treat **any** 20-byte address as a valid "ERC20 asset" — there is no whitelist restricting which Solidity contracts may be addressed through XCM, exactly the missing-whitelist root cause identified in the Tempus report ("you supply an arbitrary tempusAMM ... could be a fake contract"). On top of that, `deposit_asset_with_surplus` only ever processes the *first* fungible asset out of the `AssetsInHolding` it receives; on success it returns `Ok(surplus)` and silently drops the whole `what` value (including any un-deposited assets), because the `defensive_assert!` that is supposed to guard "single asset only" is a no-op in production (release) builds.

### Finding Description
The matcher that defines which locations are accepted as ERC20 assets is: [1](#0-0) 

`IsLocalAccountKey20` accepts *every* `Location` of the shape `(0, [AccountKey20 { .. }])` — i.e. every possible Ethereum-style 20-byte address, without any registry/whitelist of legitimate, vetted ERC20 contracts. This is used to build `ERC20Matcher`, which feeds `ERC20Transactor`: [2](#0-1) 

Both transactor methods take the caller-controlled `asset.id` (an arbitrary `AccountKey20`), turn it into an `H160`, and issue a `pallet_revive::Pallet::<T>::bare_call` to that arbitrary address, trusting only the boolean `IERC20::transferCall` return value to decide whether real value moved: [3](#0-2) [4](#0-3) 

This is invoked from a fully public, unprivileged entry point: any signed account can submit an XCM program via `PolkadotXcm::execute` that references an arbitrary attacker-deployed contract as the "asset" (as already exercised in the repo's own test using a `dummy` non-ERC20 contract): [5](#0-4) 

Independently of contract legitimacy, `deposit_asset_with_surplus` only inspects `what.fungible_assets_iter().next()` and matches a single asset, while the guard against being handed more than one asset is a `defensive_assert!`, which under `#[cfg(not(debug_assertions))]` (i.e. any production/release runtime) compiles to nothing: [6](#0-5) 

If the XCM engine ever calls this `TransactAsset` with a holding containing more than one fungible asset (e.g. via composite transactor tuples, or multi-asset `DepositAsset`/`DepositReserveAsset` execution paths that batch the filtered holding before dispatching to the configured `AssetTransactor`), only the first asset is credited to the beneficiary through a real ERC20 `transfer`. On the `Ok(true)` branch the function returns `Ok(surplus)` and the `what: AssetsInHolding` parameter — which still contains any *other* assets that were never matched/transferred — is simply dropped when the function returns, with no path to return them to holding or to the caller. Since `AssetsInHolding` has no `Drop` impl that recovers to storage, those assets are permanently destroyed/unaccounted for. This satisfies "permanent user-fund ... lock" from the required impact list, and stems directly from trusting an under-constrained, unwhitelisted parameter (the asset/contract set) exactly as in the Tempus finding, rather than from any privileged/governance misconfiguration.

### Impact Explanation
- Any unprivileged account can force the runtime to make external calls to arbitrary, unaudited contracts from the system's `TransfersCheckingAccount`, with no on-chain whitelist gating which contracts are legitimate ERC20 tokens backing real value.
- Whenever the transactor is exercised with a multi-asset holding, additional assets bundled alongside the first are silently and permanently lost — no error is raised, no funds are returned, and the loss is undetectable from the XCM error surface since the call reports success (`Ok(surplus)`).
- This combination directly parallels the accepted-but-downgraded Tempus H-01/M-severity finding: an arbitrary, unvalidated external contract parameter is trusted to gate movement/accounting of value the protocol holds.

### Likelihood Explanation
The `IsLocalAccountKey20` matcher unconditionally accepts any `AccountKey20` location — this is a deployed, non-hypothetical configuration in `asset-hub-westend` (`ERC20Matcher`/`ERC20Transactor` wired into `xcm_config.rs`). Triggering the "arbitrary contract" surface requires only a signed `pallet_xcm::execute` call, which is public and already demonstrated working (with a non-ERC20 contract) in the repo's own test suite. The multi-asset silent-drop requires the executor to invoke the transactor with more than one asset in `what`; this depends on the exact XCM executor call pattern (single-asset-per-call vs. batched), which could not be fully confirmed from the available index within the tool budget — this is the primary source of uncertainty in this finding.

### Recommendation
- Introduce an explicit on-chain whitelist/registry of approved ERC20 contract addresses that `ERC20Matcher`/`ERC20Transactor` are permitted to call, rather than accepting any `AccountKey20` location.
- Replace the `defensive_assert!(what.len() == 1, ...)` in `deposit_asset_with_surplus` with a real runtime check that returns an error (returning the untouched `what`) when more than one asset is present, instead of allowing production builds to silently drop excess assets.
- Verify actual balance deltas via `fungibles::Inspect`/real contract state rather than trusting only the boolean ERC20 return value, mirroring the Tempus fix of adding a whitelist for trusted external contracts.

### Proof of Concept
1. Attacker deploys a minimal Solidity contract implementing `transfer(address,uint256) returns (bool)` that unconditionally returns `true` without touching any storage.
2. Attacker calls `PolkadotXcm::execute` (public, unprivileged) with an XCM program:
   - `WithdrawAsset` for `Asset{ id: Location(0,[AccountKey20{key: attackerContract}]), fun: Fungible(amount) }` — this passes `IsLocalAccountKey20`/`ERC20Matcher` unchanged and drives `withdraw_asset_with_surplus`, which calls `attackerContract.transfer(checkingAddress, amount)`; the fake contract returns `true`, and `amount` units of the "asset" are credited into the XCM holding register with no real backing.
   - Followed by a further instruction (or combined with other legitimately matched assets in the same `Assets` filter) that ends up presenting `what.len() > 1` to `deposit_asset_with_surplus`.
3. `deposit_asset_with_surplus` matches and transfers only the first asset in `what`; on `Ok(true)` it returns `Ok(surplus)`, and the remaining asset(s) held in `what` are dropped — never delivered to the beneficiary, never returned to holding, and never refunded to the original owner, cf. [7](#0-6) .

Confirming the exact instruction sequence in the live `asset-hub-westend` XCM executor that produces a `what.len() > 1` call into this specific `TransactAsset` implementation would require running the executor (e.g. via a Devin session with repo access) to trace `deposit_asset` call sites end-to-end; this could not be fully verified within the available tool budget.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L109-131)
```rust
impl<
		AccountId: Eq + Clone,
		T: pallet_revive::Config<AccountId = AccountId>,
		AccountIdConverter: ConvertLocation<AccountId>,
		Matcher: MatchesFungibles<H160, u128>,
		WeightLimit: Get<Weight>,
		StorageDepositLimit: Get<BalanceOf<T>>,
		TransfersCheckingAccount: Get<AccountId>,
	> TransactAsset
	for ERC20Transactor<
		T,
		Matcher,
		AccountIdConverter,
		WeightLimit,
		StorageDepositLimit,
		AccountId,
		TransfersCheckingAccount,
	>
where
	BalanceOf<T>: Into<U256> + TryFrom<U256>,
	MomentOf<T>: Into<U256>,
	T::Hash: frame_support::traits::IsType<H256>,
{
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L218-298)
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1989-2016)
```rust
		let (code, _) = compile_module("dummy").unwrap();

		let Contract { addr: non_erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
			.build_and_unwrap_contract();

		let wnd_amount_for_fees = 1_000_000_000_000u128;
		let erc20_transfer_amount = 100u128;
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: non_erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.build();
		// Execution fails but doesn't panic.
		assert!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(2_500_000_000, 120_000),
		)
		.is_err());
	});
```
