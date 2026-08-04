## Analog Identified: `pallet-psm` Redemption Lets Callers Choose Which Backing Asset to Redeem Against With No Peg/Price Check

### Title
Unpriced per-asset redemption selection in `pallet-psm::redeem` lets a caller drain a shared reserve against an appreciated external asset - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
The Reserve Protocol bug allowed a redeemer to pick which basket (old vs. new) to redeem against, extracting an appreciated (depegged-upward) collateral at its stale 1:1-with-target accounting instead of its true market value, stealing value meant for revenue traders. `pallet-psm` reproduces the same broken invariant: `redeem()` lets the caller freely choose *which* approved `external_asset` to redeem the caller's `internal_asset` against, always at a fixed decimals-only 1:1 conversion [1](#0-0) , with **no market-price or peg-soundness check on the chosen external asset**. All externals approved on a PSM instance share the same underlying reserve account, `Self::psm_account(&internal_asset)`, used identically for both `mint`'s deposit and `redeem`'s payout [2](#0-1) [3](#0-2) , while `PsmDebt` bookkeeping is only tracked per-external-asset. This is architecturally identical to RToken's "mix of previous baskets" redemption: a single accounting rail lets the caller pick the specific collateral leg to redeem against, priced at par regardless of its actual market value.

### Finding Description
`redeem()` performs the following, keyed purely by decimals, never by price:
```
external_out = Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
``` [1](#0-0) 

The only guards before the payout are: circuit-breaker status (`allows_redemption`) [4](#0-3) , minimum swap amount, fee cap, per-external tracked debt (`current_debt >= effective_internal_net`) [5](#0-4) , and a defensive check that the *shared* reserve account actually holds enough of that external asset [6](#0-5) . None of these checks verify that the external asset's real market value is still at parity with the internal asset's peg — exactly the missing check the RToken report calls out ("check that the collateral used is sound or at least that the price isn't higher than the peg price").

Because a PSM instance can approve multiple external assets against the same internal asset and debt ceiling (`ExternalAssets`, `AssetCeilingWeight`, `PsmDebt` are all keyed by `(internal_asset, external_asset)` pairs sharing one `Psm<T>` config and one physical reserve account) [7](#0-6) , a market event that pushes one approved external above its intended $1 par (e.g. a yield-bearing/rebasing stablecoin briefly trading above peg, or an asset whose oracle-independent PSM accounting never reflects real value) allows any holder of the internal asset to redeem specifically against that asset. They extract more real value than the internal-asset amount they burned is actually worth, at the expense of the shared reserve that is meant to back *all* other externals/holders pro-rata — the PSM equivalent of RToken's revenue-trader funds being siphoned off.

### Impact Explanation
An unprivileged, unauthenticated caller (any signed account) can call the public `redeem` extrinsic and choose the specific `external_asset` leg to redeem against. If that asset's true value has drifted above its assumed 1:1 parity with the internal asset — a routine market condition for pegged assets, not requiring any admin/governance/relayer/validator misbehavior — the caller extracts value the PSM's accounting does not price. This depletes the shared reserve backing the internal asset for all other externals, breaking the PSM's fundamental over-collateralization guarantee and potentially leaving other approved externals unbacked, which is a direct fund-loss/fund-lock analog for other users of the same PSM instance.

### Likelihood Explanation
Requires only: (1) a PSM instance with two or more approved externals, and (2) one approved external asset's market value drifting above the parity the PSM assumes (no price oracle is consulted anywhere in `mint`/`redeem`). Since `pallet-psm` performs no price verification at all — by design, since it treats decimals-adjusted conversion as the sole exchange logic — any legitimate approved external asset that is not a perfectly rigid peg (e.g., interest-accruing wrapped stables, LSTs, or any asset later found to be worth more than intended) creates this condition without any attacker-controlled trigger, malicious relayer, or governance abuse.

### Recommendation
Before paying out `external_out` in `redeem`, verify that the selected `external_asset`'s current market/oracle price is not above its expected peg value (or cap `external_out` at the internal amount's true peg-equivalent value using a price feed), mirroring the RToken team's own accepted mitigation direction. Alternatively, segregate reserves and debt ceilings so each external's redemption is strictly bounded by the exact internal-asset debt it individually created, and stop allowing an arbitrary caller-selected external leg to be redeemed at a rate divorced from real value.

### Proof of Concept
1. Admin creates a PSM for `internal_asset = pUSD` and approves two externals: `USDC` (par) and `Y` (currently par, later depegs upward to 2x its registered value).
2. Users mint `pUSD` 1:1 against `USDC` deposits via `mint()` [8](#0-7) ; the PSM reserve account also accumulates `Y` from other mints.
3. `Y` depegs upward (external market event, no admin/relayer/malicious-actor action needed).
4. Attacker holding `pUSD` (obtained from the `USDC` leg or on secondary markets) calls `redeem(internal_asset=pUSD, external_asset=Y, internal_amount, max_fee)`. `internal_to_external` computes `external_out` purely from decimal ratio, ignoring that 1 unit of `Y` is now worth 2x a `pUSD` unit [9](#0-8) .
5. Attacker receives `Y` tokens worth roughly double the `pUSD` burned, draining the shared reserve account that should still be backing `USDC`-side debt and other holders — the PSM never checks `Y`'s real price before releasing it.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L459-521)
```rust
	#[pallet::storage]
	pub type PsmDebt<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		BalanceOf<T>,
		ValueQuery,
	>;

	/// Fee for external → internal swaps (minting), per `(internal, external)` pair.
	/// Defaults to 0.5%.
	#[pallet::storage]
	pub(crate) type MintingFee<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		Permill,
		ValueQuery,
		DefaultFee,
	>;

	/// Fee for internal → external swaps (redemption), per `(internal, external)` pair.
	/// Defaults to 0.5%.
	#[pallet::storage]
	pub(crate) type RedemptionFee<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		Permill,
		ValueQuery,
		DefaultFee,
	>;

	/// Per-external ceiling weight within a PSM, normalised against the sum of weights
	/// for the same instance. Zero disables minting for that external.
	#[pallet::storage]
	pub(crate) type AssetCeilingWeight<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		Permill,
		ValueQuery,
	>;

	/// Approved external assets per PSM.
	#[pallet::storage]
	pub(crate) type ExternalAssets<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		ExternalAssetInfo,
		OptionQuery,
	>;
```

**File:** substrate/frame/psm/src/lib.rs (L702-751)
```rust
		pub fn mint(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			external_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;

			let fee_rate = MintingFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_equivalent);
			let internal_to_user = internal_equivalent.saturating_sub(fee);

			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);

			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
			T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
```

**File:** substrate/frame/psm/src/lib.rs (L811-836)
```rust
		pub fn redeem(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			internal_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);

			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L848-849)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);
```

**File:** substrate/frame/psm/src/lib.rs (L851-855)
```rust
			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}
```

**File:** substrate/frame/psm/src/lib.rs (L878-887)
```rust
			let psm_account = Self::psm_account(&internal_asset);
			if !external_out.is_zero() {
				T::Fungibles::transfer(
					external_asset.clone(),
					&psm_account,
					&who,
					external_out,
					Preservation::Expendable,
				)?;
			}
```
