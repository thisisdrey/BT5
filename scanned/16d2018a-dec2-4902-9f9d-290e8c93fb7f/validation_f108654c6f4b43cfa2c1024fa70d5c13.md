## Analysis

The external report's core broken invariant: a minimum-notional gate is enforced symmetrically on entry and exit, but the *realized* value after a state-changing operation (fee/price impact) can fall below that same gate — permanently trapping the position because there is no "exit below minimum" bypass, and the only way out is to add more funds to clear the threshold.

I found a structurally identical, locally provable analog in the PSM (Peg Stability Module) pallet at `substrate/frame/psm/src/lib.rs`.

### Title
Minting fee deduction can leave a user's PSM internal-asset balance permanently below `min_swap_amount`, making it unredeemable - (File: substrate/frame/psm/src/lib.rs)

### Summary
`Pallet::mint` checks `min_swap_amount` against `internal_equivalent` (the pre-fee gross amount), but the amount actually credited to the user is `internal_equivalent - fee`. `Pallet::redeem` checks `min_swap_amount` against the caller-supplied `internal_amount` with no bypass for redeeming an account's full remaining balance. Because the fee is rounded up (`mul_ceil`, "never undercharges"), any mint whose gross amount is close to `min_swap_amount` credits the user with net internal tokens strictly below `min_swap_amount`, and that balance can never satisfy `redeem`'s own `min_swap_amount` gate — the tokens are stuck in the internal asset with no exit route back to the external reserve, exactly mirroring the reported "shares worth less than MIN_NOTIONAL_WEI can't be sold" DOS.

### Finding Description
In `mint`, the threshold check happens before the fee is taken out: [1](#0-0) 

```
let internal_equivalent = Self::external_to_internal(...)?;
ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);
...
let fee = fee_rate.mul_ceil(internal_equivalent);
let internal_to_user = internal_equivalent.saturating_sub(fee);
...
T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
``` [2](#0-1) 

`internal_to_user` (what the caller actually receives) is never re-checked against `min_swap_amount`. When the caller mints an amount whose `internal_equivalent` is at or just above `min_swap_amount`, `internal_to_user` is guaranteed to fall below it whenever the minting fee is non-zero (the default fee is 0.5%, see `DefaultFee`): [3](#0-2) 

In `redeem`, the gate is applied to the amount the caller is trying to redeem, with no "redeem entire remaining balance" exception:
```
ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);
``` [4](#0-3) 

If a user's total internal-asset holdings from this PSM are below `min_swap_amount` (as produced by the mint path above), no value of `internal_amount` they can supply will ever pass this check — they cannot redeem any of it, unless they first mint (i.e., deposit more external asset) to top the balance back above `min_swap_amount`, which is precisely the "buy more to unlock" workaround called out as unacceptable in the original report.

`min_swap_amount` is fixed per-instance at `create_psm` and is not adjustable afterward (per the pallet's own README: "The per-instance minimum swap amount is not a config constant — it is set on `create_psm`"), so this is not a privileged/governance-triggered scenario — any ordinary, unprivileged caller of `mint` with a normal non-zero fee rate can put themselves (or, since amounts are attacker-chosen, deliberately trap another address that later receives a small transfer of the internal asset) into this locked state purely through the public `mint` extrinsic.

### Impact Explanation
Funds credited by `mint` become economically stranded: the user holds a genuine, valid, non-zero internal-asset balance that is fully backed 1:1 in the PSM's reserve (per `PsmDebt`), but the *only* pallet-provided exit path (`redeem`) can never process it. This is a permanent user-fund lock inside a bridge/stability-style component, matching the "permanent user-fund or bridge-state lock" impact category. Severity is amplified because the trap is self-inflicted by ordinary use (minting near the threshold) rather than requiring any adversarial setup, and there is no governance lever to retroactively rescue already-minted dust once `min_swap_amount` has been fixed at instance creation.

### Likelihood Explanation
High. Any minting fee greater than zero (the pallet's own `DefaultFee` is 0.5%) combined with a mint whose gross internal-equivalent is between `min_swap_amount` and `min_swap_amount / (1 - fee_rate)` deterministically produces this outcome. No whale, no price movement, no validator or governance action is required — a single unprivileged `mint` call by a normal user is sufficient, and the existing test suite (`mint_min_swap_is_enforced_on_internal_side`, `redeem_rejects_when_external_out_truncates_to_zero`) shows the team tests the entry-side and truncation-side gates but not this net-of-fee interaction between `mint` and `redeem`. [5](#0-4) 

### Recommendation
Re-check the post-fee, actually-credited amount (`internal_to_user`) against `min_swap_amount` in `mint` (rejecting mints that would leave the user under the floor), and/or allow `redeem` to bypass the `min_swap_amount` floor when `internal_amount` equals the caller's entire remaining balance for that PSM/internal asset (mirroring the "full unbond always allowed" pattern used elsewhere in the codebase, e.g. `is_full_unbond` in `pallet-nomination-pools`'s `ok_to_unbond_with`). [6](#0-5) 

### Proof of Concept
1. `create_psm` is called for `INTERNAL_ASSET_ID` with `min_swap_amount = M` and default 0.5% minting fee, external asset `X` approved 1:1 decimals.
2. Attacker/user calls `Psm::mint(origin, INTERNAL_ASSET_ID, X, M, Permill::from_percent(1))` (gross external amount converts to `internal_equivalent = M`, which passes `internal_equivalent >= min_swap_amount`).
3. `fee = mul_ceil(0.5% of M) > 0`, so `internal_to_user = M - fee < M = min_swap_amount`. The user is minted `internal_to_user` tokens.
4. User calls `Psm::redeem(origin, INTERNAL_ASSET_ID, X, internal_to_user, max_fee)`.
5. `ensure!(internal_to_user >= info.min_swap_amount, Error::<T>::BelowMinimumSwap)` fails — the call always reverts with `BelowMinimumSwap`, for any `internal_amount` up to the user's full balance, permanently trapping the minted tokens. [4](#0-3)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L262-268)
```rust
	/// Suggested fee of 0.5% for minting and redemption.
	pub(crate) struct DefaultFee;
	impl Get<Permill> for DefaultFee {
		fn get() -> Permill {
			Permill::from_parts(5_000)
		}
	}
```

**File:** substrate/frame/psm/src/lib.rs (L700-756)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::mint(T::MaxExternals::get()))]
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
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```

**File:** substrate/frame/psm/src/lib.rs (L811-828)
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
```

**File:** substrate/frame/psm/src/tests.rs (L2673-2691)
```rust
	#[test]
	fn mint_min_swap_is_enforced_on_internal_side() {
		new_test_ext().execute_with(|| {
			register_external_asset_with_weight(DAI_MOCK_ASSET_ID, Permill::from_percent(100));

			// 50 DAI = 50 internal equivalent, below MinSwapAmount (100 internal).
			let below = 50 * DAI_UNIT;
			assert_noop!(
				Psm::mint(
					RuntimeOrigin::signed(ALICE),
					INTERNAL_ASSET_ID,
					DAI_MOCK_ASSET_ID,
					below,
					Permill::zero()
				),
				Error::<Test>::BelowMinimumSwap
			);
		});
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1242-1252)
```rust
		// any unbond must comply with the balance condition:
		ensure!(
			is_full_unbond ||
				balance_after_unbond >=
					if is_depositor {
						Pallet::<T>::depositor_min_bond()
					} else {
						MinJoinBond::<T>::get()
					},
			Error::<T>::MinimumBondNotMet
		);
```
