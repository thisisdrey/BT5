### Title
Permissionless `create_pool` lets an attacker squat a deterministic pool key and permanently block the canonical Asset-Hub/bridge liquidity pool for that asset pair - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion::create_pool` is a fully permissionless, unprivileged extrinsic that derives a deterministic `PoolId` purely from the two `AssetKind` values supplied, and permanently reverts with `PoolExists` if that key is already taken. This is the exact same broken invariant as the reported AutoRoller/SpaceFactory issue: a permissionless "create-once, keyed-by-deterministic-identity" function with no privileged override lets any unrelated actor front-run and permanently squat the slot the legitimate protocol setup was going to use.

### Finding Description
`create_pool` is callable by any signed account with no `AdminOrigin`/root gating: [1](#0-0) 

Internally, `do_create_pool` computes the pool's identity solely from `(asset1, asset2)` via `T::PoolLocator::pool_id`, and unconditionally reverts if that key already exists: [2](#0-1) 

The pallet's own test suite confirms this: creating the same asset pair twice (in either order) always fails with `Error::<T>::PoolExists`, and there is no mechanism to reclaim, override, or recreate the pool for that pair: [3](#0-2) 

This same primitive is relied on as critical infrastructure elsewhere in the repo:
- Asset Hub's transaction-fee-payment bootstrap helper creates the canonical native/foreign-asset pool that `asset-conversion-tx-payment` needs to let users pay fees in a foreign asset: [4](#0-3) 
- Snowbridge/BridgeHub-Westend integration tests set up the WND/bridged-asset pool the same way, via the identical permissionless `create_pool` call, for exchanging fees related to bridged asset delivery: [5](#0-4) 

Because the asset pair for these canonical pools is public/predictable ahead of time (e.g. once a new foreign asset is registered via a governance `force_create_foreign_asset` call, or once a bridged asset's `Location` is known), any unprivileged account can pay the (cheap, non-governance-gated) `PoolSetupFee` and call `create_pool(asset1, asset2)` before the intended setup transaction lands. Because `Pools::<T>::contains_key(&pool_id)` is then already `true`, the legitimate setup call for that exact pair reverts with `PoolExists` forever — there is no privileged "trusted caller can still create/override" fallback (unlike the proposed AutoRoller fix), and no way to force-replace an existing pool at that key.

This mirrors the report's root cause precisely: SpaceFactory#create only allows one pool per adapter/maturity and reverts if it exists, letting an adversary who knows the future key squat it first and permanently brick the legitimate creator's flow.

### Impact Explanation
An attacker can permanently prevent Asset Hub or a bridge-hub runtime from ever establishing the canonical liquidity pool for a specific, predictable asset pair (e.g. the pool a runtime needs for `asset-conversion-tx-payment` to accept a newly-onboarded foreign asset as a fee asset, or the WND/bridged-asset pool used in Snowbridge fee-conversion flows). Once squatted, the attacker also controls the pool's initial liquidity/composition and (until `AdminOrigin::set_pool_fee` is exercised) potentially the fee parameters, degrading pricing/availability for all subsequent users who depend on that pool for fee payment or bridge fee exchange. This is a public, underpriced (attacker only pays the flat `PoolSetupFee`) action that permanently stalls intended asset-hub/bridge economic infrastructure — matching the "public underpriced work that degrades block production or stalls bridge processing" and "permanent... bridge-state lock" impact classes.

### Likelihood Explanation
High. `create_pool` requires only a signed origin and the flat setup fee — no governance, no malicious validator/collator/relayer, no leaked keys. The target asset pair is knowable in advance from public governance proposals (asset registration) or bridge configuration announcements, giving the attacker ample lead time to front-run the legitimate `create_pool`/`add_liquidity` bootstrap sequence with a normal signed extrinsic.

### Recommendation
Do not allow arbitrary permissionless accounts to permanently reserve canonical pool identities for asset pairs that a runtime intends to bootstrap itself. Options: gate pool creation for "system-relevant" pairs (e.g. native asset vs. a newly force-created foreign asset) behind `AdminOrigin`/root as is already done for `create_pool_with_fee`; or allow a privileged/trusted caller to force-replace a squatted pool for a pair before liquidity has been meaningfully added (analogous to the `isTrusted[msg.sender]` fix adopted upstream for the AutoRoller); or reserve the deterministic pool ID space for asset pairs where at least one asset was very recently created by governance, requiring an explicit governance-authorized creation window.

### Proof of Concept
1. Governance/asset team publicly submits (or it becomes known) a proposal to `force_create_foreign_asset` for asset `X`, intending to subsequently call `pallet_asset_conversion::create_pool(native, X)` and `add_liquidity` to make `X` usable as a `asset-conversion-tx-payment` fee asset, per the pattern in `setup_pool_for_paying_fees_with_foreign_assets` (`cumulus/parachains/runtimes/assets/test-utils/src/test_cases.rs:1772-1829`).
2. After `X` is created but before the team's `create_pool` transaction is included, an attacker submits their own signed `create_pool(native, X)` extrinsic (`substrate/frame/asset-conversion/src/lib.rs:442-450`), paying only `PoolSetupFee`.
3. `do_create_pool` computes the same deterministic `pool_id` from `(native, X)`, inserts it into `Pools`, and emits `PoolCreated` with the attacker as owner (`substrate/frame/asset-conversion/src/lib.rs:729-788`).
4. The team's subsequent `create_pool(native, X)` call now hits `ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists)` and reverts permanently — reproduced deterministically by `create_same_pool_twice_should_fail` (`substrate/frame/asset-conversion/src/tests.rs:246-285`), which shows the pool-exists revert triggers regardless of asset ordering and persists indefinitely, with no code path to reclaim or override the pool.

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

**File:** substrate/frame/asset-conversion/src/tests.rs (L246-285)
```rust
#[test]
fn create_same_pool_twice_should_fail() {
	new_test_ext().execute_with(|| {
		let user = 1;
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);

		create_tokens(user, vec![token_2.clone()]);

		let lp_token = AssetConversion::get_next_pool_asset_id();
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_2.clone()),
			Box::new(token_1.clone())
		));
		let expected_free = lp_token + 1;
		assert_eq!(expected_free, AssetConversion::get_next_pool_asset_id());

		assert_noop!(
			AssetConversion::create_pool(
				RuntimeOrigin::signed(user),
				Box::new(token_2.clone()),
				Box::new(token_1.clone())
			),
			Error::<Test>::PoolExists
		);
		assert_eq!(expected_free, AssetConversion::get_next_pool_asset_id());

		// Try switching the same tokens around:
		assert_noop!(
			AssetConversion::create_pool(
				RuntimeOrigin::signed(user),
				Box::new(token_1.clone()),
				Box::new(token_2.clone())
			),
			Error::<Test>::PoolExists
		);
		assert_eq!(expected_free, AssetConversion::get_next_pool_asset_id());
	});
}
```

**File:** cumulus/parachains/runtimes/assets/test-utils/src/test_cases.rs (L1772-1829)
```rust
pub fn setup_pool_for_paying_fees_with_foreign_assets<Runtime, RuntimeOrigin>(
	existential_deposit: Balance,
	(foreign_asset_owner, foreign_asset_id_location, foreign_asset_id_minimum_balance): (
		AccountId,
		Location,
		Balance,
	),
) where
	Runtime: frame_system::Config<RuntimeOrigin = RuntimeOrigin, AccountId = AccountId>
		+ pallet_balances::Config<Balance = u128>
		+ pallet_assets::Config<
			pallet_assets::Instance2,
			AssetId = xcm::v5::Location,
			Balance = <Runtime as pallet_balances::Config>::Balance,
		> + pallet_asset_conversion::Config<
			AssetKind = xcm::v5::Location,
			Balance = <Runtime as pallet_balances::Config>::Balance,
		>,
	RuntimeOrigin: OriginTrait<AccountId = <Runtime as frame_system::Config>::AccountId>,
	<<Runtime as frame_system::Config>::Lookup as StaticLookup>::Source:
		From<<Runtime as frame_system::Config>::AccountId>,
{
	// setup a pool to pay fees with `foreign_asset_id_location` tokens
	let pool_owner: AccountId = [14u8; 32].into();
	let native_asset = Location::parent();
	let pool_liquidity: Balance =
		existential_deposit.max(foreign_asset_id_minimum_balance).mul(100_000);

	let _ = pallet_balances::Pallet::<Runtime>::force_set_balance(
		RuntimeOrigin::root(),
		pool_owner.clone().into(),
		(existential_deposit + pool_liquidity).mul(2).into(),
	);

	assert_ok!(pallet_assets::Pallet::<Runtime, pallet_assets::Instance2>::mint(
		RuntimeOrigin::signed(foreign_asset_owner),
		foreign_asset_id_location.clone().into(),
		pool_owner.clone().into(),
		(foreign_asset_id_minimum_balance + pool_liquidity).mul(2).into(),
	));

	assert_ok!(pallet_asset_conversion::Pallet::<Runtime>::create_pool(
		RuntimeOrigin::signed(pool_owner.clone()),
		Box::new(native_asset.clone().into()),
		Box::new(foreign_asset_id_location.clone().into())
	));

	assert_ok!(pallet_asset_conversion::Pallet::<Runtime>::add_liquidity(
		RuntimeOrigin::signed(pool_owner.clone()),
		Box::new(native_asset.into()),
		Box::new(foreign_asset_id_location.into()),
		pool_liquidity,
		pool_liquidity,
		1,
		1,
		pool_owner,
	));
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_common.rs (L443-503)
```rust
// set up pool
pub(crate) fn set_up_pool_with_wnd_on_ah_westend(
	asset: Location,
	is_foreign: bool,
	initial_fund: u128,
	initial_liquidity: u128,
) {
	let wnd: Location = Parent.into();
	AssetHubWestend::fund_accounts(vec![(AssetHubWestendSender::get(), initial_fund)]);
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		let owner = AssetHubWestendSender::get();
		let signed_owner = <AssetHubWestend as Chain>::RuntimeOrigin::signed(owner.clone());

		if is_foreign {
			assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::ForeignAssets::mint(
				signed_owner.clone(),
				asset.clone().into(),
				owner.clone().into(),
				initial_fund,
			));
		} else {
			let asset_id = match asset.interior.last() {
				Some(GeneralIndex(id)) => *id as u32,
				_ => unreachable!(),
			};
			assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::Assets::mint(
				signed_owner.clone(),
				asset_id.into(),
				owner.clone().into(),
				initial_fund,
			));
		}
		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::AssetConversion::create_pool(
			signed_owner.clone(),
			Box::new(wnd.clone()),
			Box::new(asset.clone()),
		));
		assert_expected_events!(
			AssetHubWestend,
			vec![
				RuntimeEvent::AssetConversion(pallet_asset_conversion::Event::PoolCreated { .. }) => {},
			]
		);
		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::AssetConversion::add_liquidity(
			signed_owner.clone(),
			Box::new(wnd),
			Box::new(asset),
			initial_liquidity,
			initial_liquidity,
			1,
			1,
			owner.into()
		));
		assert_expected_events!(
			AssetHubWestend,
			vec![
				RuntimeEvent::AssetConversion(pallet_asset_conversion::Event::LiquidityAdded {..}) => {},
			]
		);
	});
```
