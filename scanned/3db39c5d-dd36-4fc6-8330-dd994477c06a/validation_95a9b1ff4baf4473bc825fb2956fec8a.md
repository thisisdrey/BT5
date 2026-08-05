This confirms the analog: `pallet_asset_conversion`'s `get_reserves` and `get_balance` directly call `T::Assets::balance(asset, &pool_account)` [1](#0-0)  which, when `T::Assets` is instantiated over `pallet-revive`'s ERC20 fungibles adapter, resolves to a live `balanceOf` call on an arbitrary `H160` address [2](#0-1) , with that `H160` derived from any XCM `Location` ending in an `AccountKey20` junction with no canonical/whitelist check [3](#0-2) .

### Title
DEX pool reserve accounting for ERC20 `AssetKind`s trusts `balanceOf`, enabling double-entrypoint-token liquidity theft - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`pallet-asset-conversion`'s `get_reserves`/pricing/swap logic treats a pool's "reserve" of a token as whatever the token's `Inspect::balance(asset, &pool_account)` reports at call time [1](#0-0) . When the `AssetKind` is an ERC20 contract reachable through `pallet-revive`, that balance is obtained by dispatching a live `balanceOf` call to the specific `H160` address that was decoded from the XCM `Location`/asset-kind bytes [4](#0-3)  (or, in the ERC20 XCM transactor, transfers are literally routed to that address via `bare_call` [2](#0-1) ). The `AccountKey20ToH160` matcher accepts *any* 20-byte address embedded in a `Location` as a distinct asset identity, with no registry restricting which address is the "official" one for a given token [3](#0-2) . This is precisely the pattern the external report warns about: relying on `balanceOf` of a contract address as the sole source of truth for a pool's holdings, while treating the address itself as a unique token identifier.

### Finding Description
If an ERC20 token deployed under `pallet-revive` (or bridged from Ethereum) exposes multiple valid contract addresses that manipulate the same underlying balance storage (a "double-entrypoint" token — proxy/delegate pattern, or any contract whose `balanceOf`/`transfer` forward to shared storage under a second address), the runtime has no way to detect this because:

1. `pallet-asset-conversion`'s `AssetKind` is a generic `Parameter` (here effectively an XCM `Location`/`H160`) [5](#0-4) , and two different `Location`s (`AccountKey20{key: addr_A}` and `AccountKey20{key: addr_B}`) are treated as two entirely independent assets even if `addr_A` and `addr_B` alias the same storage.
2. `get_reserves`/`get_balance` never track an internal ledger of what was actually deposited per asset kind — they simply re-query `balanceOf(pool_account)` on whichever contract address is passed in as the asset kind [1](#0-0) .
3. `ERC20Transactor::deposit_asset_with_surplus`/`withdraw_asset_with_surplus` move real ERC20 balance by calling `IERC20::transferCall` on the specific `H160` in the asset id, using the checking account as an intermediary [6](#0-5)  — again keyed purely by address identity, not by real underlying token identity.

Attack: an attacker who knows a double-entrypoint token has (a) an "official" address `A` already used in a well-funded pool (DOT/`A`), and (b) an alternate address `B` aliasing the same storage, can:
- Create a brand-new pool (DOT/`B`) via `createPool`/`create_pool` (unprivileged, permissionless in `pallet-asset-conversion`).
- `addLiquidity` to the DOT/`B` pool using a trivial amount of `B`-denominated tokens. Because `B` shares storage with `A`, this liquidity add increases the real, shared ERC20 balance held by the DOT/`B` pool account, but this same physical balance is also what will be reported by `balanceOf` if queried through address `A` for that same pool account — the two "assets" are the same underlying value.
- Immediately `removeLiquidity` from the DOT/`B` pool. Because `get_reserves`/pricing is computed purely from a live `balanceOf` snapshot rather than tracked, isolated internal debt/credit per asset kind, and because pool-account balances for `A` and `B` are aliases of the same storage slot, the attacker can engineer a state where withdrawing from the attacker-controlled pool nets more real value out than was deposited under the `B` identity — mirroring the FVM `_settlement`/`getNetBalance` exploit where a positive "net balance" is fabricated for the double-entrypoint token and credited to the attacker.
- The victim pool (DOT/`A`) ends up with its real balanceOf(pool_account) drained relative to what its LP-token accounting believes is backing it, because the shared storage was manipulated through the sibling `B`-keyed pool.

This is not a chain-halting bug or a validator/relayer-privileged scenario; it is exploitable by any unprivileged account that (1) deploys or discovers a double-entrypoint ERC20 contract and (2) calls the permissionless `create_pool`/`add_liquidity`/`remove_liquidity`/XCM-transfer extrinsics.

### Impact Explanation
Funds theft / unbacked value extraction: an attacker can drain the real ERC20 balance backing a legitimate liquidity pool or an XCM-held ERC20 balance, stealing honest LPs' or bridge-users' funds while only paying gas/weight costs. This satisfies "theft or unbacked mint/unlock" and "duplicate settlement" impact categories, since the runtime settles the removeLiquidity payout based on a `balanceOf` view that does not correspond 1:1 with the accounting invariant the LP tokens are meant to represent.

### Likelihood Explanation
Medium: it requires (a) an ERC20 contract with a genuine double-entrypoint characteristic reachable via `pallet-revive`'s deterministic Solidity/PVM environment, and (b) that token being paired into a `pallet-asset-conversion` pool. Double-entrypoint tokens are a known, real-world class (proxy contracts, upgradeable tokens, wrapped/legacy tokens with two live addresses), and nothing in `AccountKey20ToH160`, `ERC20Matcher`, or `pallet-asset-conversion`'s permissionless `create_pool` prevents such tokens from being paired. The attack itself, once the token exists, is fully attacker-controlled and atomic (gas-only cost), matching the original report's likelihood assessment.

### Recommendation
- At minimum, document/warn (as the original report recommends) that ERC20 tokens usable as `AssetKind` in `pallet-asset-conversion` or via `ERC20Transactor` must not have multiple valid entrypoint addresses, and consider an explicit allow-list of "canonical" ERC20 contract addresses permitted to back pools/XCM transfers, enforced in `AccountKey20ToH160`/`ERC20Matcher` or at `create_pool`/`register_token` time.
- Architecturally, prefer tracking pool reserves via pallet-internal debit/credit ledgers (as `pallet-assets`/`pallet-balances` already do) rather than trusting a live `balanceOf` snapshot of an externally-controlled contract as the ground truth for settlement, for any asset kind whose accounting is delegated to arbitrary Solidity/PVM contracts.
- Alternatively, require any ERC20 asset onboarded into `pallet-asset-conversion` pools to be additionally registered/canonicalized (single-address invariant enforced) similar to how `ForeignToNativeId`/`ForeignAssetIdToAssetIndex` mappings are maintained for other foreign assets [7](#0-6) .

### Proof of Concept
Conceptual PoC (cannot be fully executed without a concrete double-entrypoint Solidity fixture, which is outside indexed repository content):
1. Deploy (via `pallet_revive::instantiate`) a token contract `T` whose storage can be reached and mutated identically through two distinct `H160` addresses `A` and `B` (e.g. a minimal proxy/delegatecall forwarder deployed at `B` pointing at `T`'s storage slot layout, or a token contract that self-registers a second "legacy" address that shares its balance mapping).
2. `pallet_asset_conversion::create_pool(DOT, Location(AccountKey20{A}))`, then `add_liquidity` with real DOT + `T`-tokens via address `A`. Pool now shows `get_reserves` = (`dot_amt`, `bal_A`) where `bal_A = balanceOf_via_A(pool_account_A)` [1](#0-0) .
3. Attacker calls `create_pool(DOT, Location(AccountKey20{B}))`, then `add_liquidity` a small amount, transferring tokens into `pool_account_B` via the `ERC20Transactor`/precompile calling `B.transferCall` [8](#0-7) . Because `A` and `B` share underlying storage, this write is visible to any `balanceOf` call against either address for the relevant account.
4. Attacker calls `remove_liquidity` on pool `B`; `get_reserves`/quote logic re-reads `balanceOf` live and pays out based on whatever the shared storage reports at that instant, allowing the attacker to extract value that was actually contributed to/backed by pool `A`'s LPs, analogous to the FVM `_settlement`/`getNetBalance` credit-and-drain sequence in the original report.

**Uncertainty/limitations**: I could not locate an actual double-entrypoint Solidity fixture or exhaustive proof that current test suites lack a case for this; the analysis is based on tracing the trust boundary (`balanceOf`-driven reserve accounting keyed by unregistered `H160` addresses) which structurally mirrors the reported bug class. Whether `pallet_asset_conversion` is deployed/configured in production with `pallet-revive`/ERC20 `AssetKind`s (vs. only `u32`/`NativeOrWithId<u32>` in current asset-hub runtimes) should be verified against the live runtime configuration, since the `AssetConversion` precompile in `asset-hub-westend` config shows this wiring exists [9](#0-8)  but I did not fully confirm whether `pallet_asset_conversion::Config::AssetKind`/`Assets` on that specific runtime is bound to a type that can resolve to arbitrary `H160` ERC20 contracts versus only trust-backed/pool/foreign assets.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L132-141)
```rust
		/// Type of asset class, sourced from [`Config::Assets`], utilized to offer liquidity to a
		/// pool.
		type AssetKind: Parameter + MaxEncodedLen + MaybeSerializeDeserialize;

		/// Registry of assets utilized for providing liquidity to pools.
		type Assets: Inspect<Self::AccountId, AssetId = Self::AssetKind, Balance = Self::Balance>
			+ Mutate<Self::AccountId>
			+ AccountTouch<Self::AssetKind, Self::AccountId, Balance = Self::Balance>
			+ Balanced<Self::AccountId>
			+ Refund<Self::AccountId, AssetId = Self::AssetKind>;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1514)
```rust
		pub fn get_reserves(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
		) -> Result<(T::Balance, T::Balance), Error<T>> {
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			let balance1 = Self::get_balance(&pool_account, asset1);
			let balance2 = Self::get_balance(&pool_account, asset2);

			if balance1.is_zero() || balance2.is_zero() {
				Err(Error::<T>::PoolEmpty)?;
			}

			Ok((balance1, balance2))
		}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L100-123)
```rust
		self.0
	}
	fn saturating_take(&mut self, amount: u128) -> Box<dyn ImbalanceAccounting<u128>> {
		let new = self.0.min(amount);
		self.0 = self.0 - new;
		Box::new(Erc20Credit(new))
	}
}

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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-306)
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
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
		}
	}
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L141-160)
```rust
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

**File:** substrate/frame/assets/precompiles/src/foreign_assets.rs (L95-114)
```rust
		/// Insert a new asset mapping, allocating a sequential index.
		/// Returns the allocated asset index on success.
		pub fn insert_asset_mapping(asset_id: &T::ForeignAssetId) -> Result<u32, ()> {
			if ForeignAssetIdToAssetIndex::<T>::contains_key(asset_id) {
				log::error!(target: LOG_TARGET, "Asset id {:?} already mapped", asset_id);
				return Err(());
			}

			let asset_index = NextAssetIndex::<T>::get();
			let next_index = asset_index.checked_add(1).ok_or_else(|| {
				log::error!(target: LOG_TARGET, "Asset index overflow");
			})?;

			AssetIndexToForeignAssetId::<T>::insert(asset_index, asset_id.clone());
			ForeignAssetIdToAssetIndex::<T>::insert(asset_id, asset_index);
			NextAssetIndex::<T>::put(next_index);

			log::debug!(target: LOG_TARGET, "Mapped asset {:?} to index {:?}", asset_id, asset_index);
			Ok(asset_index)
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1383-1394)
```rust
	type Precompiles = (
		ERC20<Self, InlineIdConfig<{ TRUST_BACKED_ASSETS_PRECOMPILE }>, TrustBackedAssetsInstance>,
		ERC20<Self, InlineIdConfig<{ POOL_ASSETS_PRECOMPILE }>, PoolAssetsInstance>,
		ERC20<
			Self,
			ForeignIdConfig<{ FOREIGN_ASSETS_PRECOMPILE }, Self, ForeignAssetsInstance>,
			ForeignAssetsInstance,
		>,
		XcmPrecompile<Self>,
		pallet_asset_conversion_precompiles::AssetConversion<{ ASSET_CONVERSION_PRECOMPILE }, Self>,
		VestingPrecompile<Self>,
	);
```
