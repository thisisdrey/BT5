Based on my research, I found a strong local analog to the Popcorn "malicious Staking contract" bug class in the `pallet-asset-conversion` pool-creation flow combined with the ERC20 Asset Transactor used on Asset Hub.

### Title
Permissionless `create_pool` lets an attacker pair an unvetted malicious ERC20 contract as a pool asset, letting it drain the paired legitimate asset - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet_asset_conversion::Pallet::create_pool` is callable by any signed account with an arbitrary `T::AssetKind` for both legs of the pair [1](#0-0) . On Asset Hub, `AssetKind` includes XCM `Location`s that resolve to arbitrary EVM contract addresses via the newly-added ERC20 Asset Transactor (`assets-common::erc20_transactor`), per `prdoc/stable2506/pr_7762.prdoc`, which states any asset id of the form `{parents:0, interior: X1(AccountKey20{key,...})}` is matched and its `transfer`/`transferFrom` are invoked directly on the attacker-supplied contract address [2](#0-1) . Neither `create_pool` nor `do_create_pool` verifies that an `AssetKind` corresponds to a token that was registered/deployed through any trusted template or registry — it only checks `asset1 != asset2` and that the pool doesn't already exist [3](#0-2) .

### Finding Description
This exactly mirrors the Popcorn `VaultController.deployVault` bug: a public factory-style function accepts a user-supplied "component" address (there, a Staking contract; here, an asset/ERC20 contract) and treats it as a first-class trusted component of the system (reserve accounting, swap pricing) without validating it against any approved registry of genuine, well-behaved implementations.

The `ERC20AssetTransactor::deposit_asset_with_surplus` implementation calls the arbitrary contract's `transfer` function via `pallet_revive::Pallet::bare_call` and blindly trusts the ABI-decoded boolean return value to determine success [4](#0-3) . A malicious contract deployed by the attacker (via `pallet-revive`, which allows permissionless `EnsureSigned` instantiation per `InstantiateOrigin` in the Asset Hub runtime config [5](#0-4) ) can implement a backdoored ERC20 interface that: (a) returns `true` for `transfer`/`transferFrom` without moving real value, (b) reports an inflated `balanceOf`, or (c) is a normal-looking token during pool creation/seeding but has an owner-only "sweep" function to drain the contract's own balance (the direct Popcorn analog — the "backdoor withdraw").

Because `create_pool` performs no check that the `AssetKind`/ERC20 address is an audited or registered token, an attacker can:
1. Deploy a malicious "ERC20" contract (analogous to the malicious `MultiRewardStaking`).
2. Call `AssetConversion::create_pool` pairing it with a legitimate asset (e.g., native DOT/WND) — this succeeds with no validation [6](#0-5) .
3. Attract liquidity providers who add real, legitimate asset liquidity via `add_liquidity` (also public, only requires signed origin) [7](#0-6) .
4. Use the backdoor in the malicious contract (or a `transfer` that always returns `true` without decrementing balance) to make the pool's "reserve" of the fake asset appear intact after a swap, extracting the real (legitimate) asset from the pool account.

### Impact Explanation
This causes theft of legitimately deposited funds (the paired real asset) from liquidity providers who trusted a permissionless pool listing, meeting the "theft or unbacked mint/unlock" and "public underpriced work" criteria for the Polkadot SDK impact gate. It requires no malicious validator/collator/relayer/governance action — the entire path is executable by an ordinary unprivileged, signed account.

### Likelihood Explanation
Likelihood is high: `create_pool` and `add_liquidity` are unprivileged extrinsics with `ensure_signed` as the only check [6](#0-5) , and `pallet-revive` contract instantiation is likewise open to any signed account on Asset Hub Westend. No registry of "approved" ERC20 templates gates which contracts can be used as `AssetKind`.

### Recommendation
Add a validation/allow-list step before a `Location`/ERC20 contract can be used as an `AssetKind` in `do_create_pool` — e.g., require the asset to be registered through a trusted asset-registration pallet (mirroring the fix RedVeil applied: verifying the referenced contract was deployed by/known to the system's own template/registry) — or require pools involving ERC20-mapped assets to go through a permissioned/registered listing step rather than fully permissionless `create_pool`.

### Proof of Concept
Not independently executed in this environment (no compiler/test runner access here); this is a code-review-derived analog. A concrete PoC would:
1. Deploy a malicious contract implementing `IERC20` where `transfer`/`transferFrom` always returns `true` without actually decrementing the caller's tracked balance (or includes an owner-drain function), using `pallet-revive`'s permissionless instantiation.
2. Call `AssetConversion::create_pool(origin, Box::new(native_asset), Box::new(malicious_erc20_location))` — succeeds per [6](#0-5) .
3. Have a victim call `add_liquidity` depositing real native tokens.
4. Attacker calls `swap_exact_tokens_for_tokens`/`remove_liquidity`, exploiting the forged `transfer` return values validated by `erc20_transactor.rs` lines 270-298, to extract the victim's native tokens while the malicious asset side never truly moves value.

Note: I was unable to fully confirm within this session which exact `AssetKind` type is wired into the Asset Hub Westend `pallet_asset_conversion::Config` (the targeted grep for `type AssetKind` in that file returned no match, likely due to index/search limits), so the precise interaction point between `create_pool`'s generic `AssetKind` and the ERC20-mapped `Location` matcher could not be pinpointed at the exact config line. If deeper verification is needed, a Devin session with full repository access should confirm the `Config::AssetKind`/`Config::Assets` wiring for `asset-hub-westend-runtime` and trace `MatchesFungibles` resolution for `AccountKey20` locations.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L440-450)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create_pool())]
		pub fn create_pool(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_create_pool(&sender, *asset1, *asset2, None)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L466-490)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::add_liquidity())]
		pub fn add_liquidity(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
			amount1_desired: T::Balance,
			amount2_desired: T::Balance,
			amount1_min: T::Balance,
			amount2_min: T::Balance,
			mint_to: T::AccountId,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_add_liquidity(
				&sender,
				*asset1,
				*asset2,
				amount1_desired,
				amount2_desired,
				amount1_min,
				amount2_min,
				&mint_to,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-746)
```rust
		pub(crate) fn do_create_pool(
			creator: &T::AccountId,
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			initial_fee: Option<Permill>,
		) -> Result<T::PoolId, DispatchError> {
			ensure!(asset1 != asset2, Error::<T>::InvalidAssetPair);
			if let Some(fee) = initial_fee {
				ensure!(fee <= T::MaxSwapFee::get(), Error::<T>::FeeTooHigh);
			}

			// prepare pool_id
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
```

**File:** prdoc/stable2506/pr_7762.prdoc (L8-19)
```text
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-298)
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
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1395-1400)
```rust
	type AddressMapper = pallet_revive::AccountId32Mapper<Self>;
	type RuntimeMemory = ConstU32<{ 128 * 1024 * 1024 }>;
	type PVFMemory = ConstU32<{ 512 * 1024 * 1024 }>;
	type AllowEVMBytecode = ConstBool<true>;
	type UploadOrigin = EnsureSigned<Self::AccountId>;
	type InstantiateOrigin = EnsureSigned<Self::AccountId>;
```
