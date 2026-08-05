This confirms the analog: `pallet-assets::create` is a permissionless, public dispatchable requiring only a deposit, and it sets the caller-designated `admin` as the asset's `issuer`, `admin`, and `freezer` simultaneously.### Title
Permissionless `pallet-asset-conversion` pool creation lets an unprivileged asset creator become the pool's `freezer` and permanently DoS a liquidity pool - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`do_create_pool` in `pallet-asset-conversion` accepts any two `T::AssetKind` values a signed caller supplies, with no check on who controls the administrative roles of those assets. [1](#0-0)  When the asset kind is a `pallet-assets` asset, `pallet-assets::create` is itself a fully permissionless, public dispatchable (`CreateOrigin = EnsureSigned`) that only requires a deposit, and it makes the caller-chosen `admin` account simultaneously the asset's `issuer`, `admin`, **and `freezer`**. [2](#0-1)  The `freezer` role can later call `freeze`/`block` to disallow transfers from a specific account — including the AMM pool's own account — with no protocol-level restriction preventing that account from being the pool address. [3](#0-2) [4](#0-3) 

This is the exact structural analog of the external report: a permissionless, third-party-controlled "freeze authority" on the token that a shared pool/vault relies on, with no validation that this authority is absent or benign before the pool exposes shared user funds to it.

### Finding Description
1. Any signed account can call `pallet_assets::create(origin, id, admin, min_balance)` and pay `AssetDeposit` to become the asset's `owner`, and can set `admin` (itself, typically) as `issuer`/`admin`/`freezer` in one atomic step. [5](#0-4) 
2. Any signed account can then call `pallet_asset_conversion::create_pool(origin, asset1, asset2)`, pairing this attacker-controlled asset with a legitimate asset (e.g. the native token or another sufficient asset). `do_create_pool` performs no check on the asset's `freezer`/`admin` roles — it only checks the pair is distinct, unused, and the setup fee is paid. [1](#0-0) 
3. Other unsuspecting LPs can call `add_liquidity` to this pool, depositing real value (native tokens or other assets) into the pool account alongside the attacker's asset. [6](#0-5) 
4. The attacker, as `freezer` of their own asset, calls `Assets::freeze(origin, id, pool_account)` (or `block`) against the pool's account. [7](#0-6)  Once frozen, `reducible_balance`/transfer checks reject any transfer out of that account for that asset (`Error::Frozen`), as demonstrated by `transferring_from_frozen_account_should_not_work` / `freezer_should_work`. [8](#0-7) [4](#0-3) 
5. Because `pallet-asset-conversion` swaps and `remove_liquidity` both need to move the frozen asset out of the pool account, every swap through this pool and every LP's `remove_liquidity` for this pair now fails, permanently locking the co-mingled native/legitimate-asset side of the pool as well — since a pool position cannot be partially withdrawn per-asset.

No existing guard in `do_create_pool`, `do_add_liquidity`, or `do_swap` validates that the assets being paired have no freezer, or that the freezer is a trusted/neutral entity; the pallet simply trusts `T::Assets` to behave like a "safe" fungible, exactly the assumption the external report calls out for the Solana mint's `freeze_authority`.

### Impact Explanation
This is a public, chain-native pathway to a permanent denial-of-service and fund lock for a permissionless liquidity pool: legitimate LPs who deposit real value (e.g., native DOT/WND, or trusted assets) into a pool paired against an attacker's freely-created asset can have their liquidity position permanently frozen and unrecoverable, with no admin recourse (the `thaw` call must also come from that asset's `admin`, which is the attacker). This matches "permanent user-fund lock" and "public underpriced work" degrading protocol usability in the Impact Gate.

### Likelihood Explanation
Likelihood is high: creating an asset via `pallet_assets::create` and pairing it in a pool via `create_pool` are both unprivileged, public, low-cost operations requiring only the standard `AssetDeposit` and `PoolSetupFee`. No governance, validator, collator, or leaked-key assumption is needed — the attacker is an ordinary user exploiting the intended permissionless asset/pool creation flow.

### Recommendation
- In `do_create_pool` (and/or `do_add_liquidity`), reject asset kinds whose backing `fungibles::roles::Inspect` reports a `freezer`/`admin` distinct from a neutral/no-op value, or require that pools only accept assets flagged as "sufficient"/trusted via a runtime-configured allowlist.
- Alternatively, have the pool account acquire/require freezer immunity — e.g., disallow `Assets::freeze`/`block` calls from succeeding against accounts that are registered `pallet-asset-conversion` pool accounts, verified via `T::PoolLocator`.
- At minimum, document and gate permissionless pool creation for `pallet-assets`-backed `AssetKind`s behind a check that the asset's freezer role has been renounced (e.g., set to a burn address) before it is eligible for AMM pairing.

### Proof of Concept
1. Attacker calls `Assets::create(signed(attacker), id=42, admin=attacker, min_balance=1)`, paying `AssetDeposit`; attacker becomes `issuer`/`admin`/`freezer` of asset 42. [2](#0-1) 
2. Attacker mints supply of asset 42 to themselves via `Assets::mint`.
3. Attacker calls `AssetConversion::create_pool(signed(attacker), Native, WithId(42))`, creating pool account `P`. [9](#0-8) 
4. Victim LP calls `AssetConversion::add_liquidity(signed(victim), Native, WithId(42), amount1, amount2, ...)`, depositing native tokens and asset-42 into `P`. [10](#0-9) 
5. Attacker calls `Assets::freeze(signed(attacker), 42, P)` — succeeds since attacker is the freezer of asset 42, and the account `P` already holds a balance of it (`Assets::freeze` only requires an existing `Account` entry). [7](#0-6) 
6. Any subsequent `swap_exact_tokens_for_tokens`/`remove_liquidity` involving this pool fails with `Error::Frozen` from `pallet-assets`, matching the pattern verified in `transferring_from_frozen_account_should_not_work`. [4](#0-3)  The victim's native-token liquidity in `P` is now unrecoverable — the pool position cannot be unwound because `remove_liquidity` must transfer both assets out of `P` atomically.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L452-490)
```rust
		/// Provide liquidity into the pool of `asset1` and `asset2`.
		/// NOTE: an optimal amount of asset1 and asset2 will be calculated and
		/// might be different than the provided `amount1_desired`/`amount2_desired`
		/// thus you should provide the min amount you're happy to provide.
		/// Params `amount1_min`/`amount2_min` represent that.
		/// `mint_to` will be sent the liquidity tokens that represent this share of the pool.
		///
		/// NOTE: when encountering an incorrect exchange rate and non-withdrawable pool liquidity,
		/// batch an atomic call with [`Pallet::add_liquidity`] and
		/// [`Pallet::swap_exact_tokens_for_tokens`] or [`Pallet::swap_tokens_for_exact_tokens`]
		/// calls to render the liquidity withdrawable and rectify the exchange rate.
		///
		/// Once liquidity is added, someone may successfully call
		/// [`Pallet::swap_exact_tokens_for_tokens`].
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-759)
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

			// pay the setup fee
			let fee =
				Self::withdraw(T::PoolSetupFeeAsset::get(), creator, T::PoolSetupFee::get(), true)?;
			T::PoolSetupFeeTarget::on_unbalanced(fee);

			if T::Assets::should_touch(asset1.clone(), &pool_account) {
				T::Assets::touch(asset1.clone(), &pool_account, creator)?
			};

			if T::Assets::should_touch(asset2.clone(), &pool_account) {
				T::Assets::touch(asset2.clone(), &pool_account, creator)?
			};
```

**File:** substrate/frame/assets/src/lib.rs (L843-889)
```rust
		pub fn create(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			admin: AccountIdLookupOf<T>,
			min_balance: T::Balance,
		) -> DispatchResult {
			let id: T::AssetId = id.into();
			let owner = T::CreateOrigin::ensure_origin(origin, &id)?;
			let admin = T::Lookup::lookup(admin)?;

			ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
			ensure!(!min_balance.is_zero(), Error::<T, I>::MinBalanceZero);

			if let Some(next_id) = T::AssetIdAllocator::next() {
				ensure!(id == next_id, Error::<T, I>::BadAssetId);
			}

			let deposit = T::AssetDeposit::get();
			T::Currency::reserve(&owner, deposit)?;

			Asset::<T, I>::insert(
				id.clone(),
				AssetDetails {
					owner: owner.clone(),
					issuer: admin.clone(),
					admin: admin.clone(),
					freezer: admin.clone(),
					supply: Zero::zero(),
					deposit,
					min_balance,
					is_sufficient: false,
					accounts: 0,
					sufficients: 0,
					approvals: 0,
					status: AssetStatus::Live,
				},
			);
			ensure!(T::CallbackHandle::created(&id, &owner).is_ok(), Error::<T, I>::CallbackFailed);
			T::AssetIdAllocator::advance().map_err(|_| Error::<T, I>::AssetIdAllocationFailed)?;
			Self::deposit_event(Event::Created {
				asset_id: id,
				creator: owner.clone(),
				owner: admin,
			});

			Ok(())
		}
```

**File:** substrate/frame/assets/src/lib.rs (L1180-1216)
```rust
		/// Disallow further unprivileged transfers of an asset `id` from an account `who`. `who`
		/// must already exist as an entry in `Account`s of the asset. If you want to freeze an
		/// account that does not have an entry, use `touch_other` first.
		///
		/// Origin must be Signed and the sender should be the Freezer of the asset `id`.
		///
		/// - `id`: The identifier of the asset to be frozen.
		/// - `who`: The account to be frozen.
		///
		/// Emits `Frozen`.
		///
		/// Weight: `O(1)`
		#[pallet::call_index(11)]
		pub fn freeze(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			who: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let id: T::AssetId = id.into();

			let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
			ensure!(
				d.status == AssetStatus::Live || d.status == AssetStatus::Frozen,
				Error::<T, I>::IncorrectStatus
			);
			ensure!(origin == d.freezer, Error::<T, I>::NoPermission);
			let who = T::Lookup::lookup(who)?;

			Account::<T, I>::try_mutate(&id, &who, |maybe_account| -> DispatchResult {
				maybe_account.as_mut().ok_or(Error::<T, I>::NoAccount)?.status =
					AccountStatus::Frozen;
				Ok(())
			})?;

			Self::deposit_event(Event::<T, I>::Frozen { asset_id: id, who });
			Ok(())
```

**File:** substrate/frame/assets/src/tests.rs (L1036-1051)
```rust
#[test]
fn transferring_from_frozen_account_should_not_work() {
	build_and_execute(|| {
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 2, 100));
		assert_eq!(Assets::balance(0, 1), 100);
		assert_eq!(Assets::balance(0, 2), 100);
		assert_ok!(Assets::freeze(RuntimeOrigin::signed(1), 0, 2));
		// can transfer to `2`
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50));
		// cannot transfer from `2`
		assert_noop!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 25), Error::<Test>::Frozen);
		assert_eq!(Assets::balance(0, 1), 50);
		assert_eq!(Assets::balance(0, 2), 150);
	});
```

**File:** substrate/frame/assets/src/functions.rs (L252-257)
```rust
		let details = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);

		let account = Account::<T, I>::get(&id, who).ok_or(Error::<T, I>::NoAccount)?;
		ensure!(!account.status.is_frozen(), Error::<T, I>::Frozen);

```
