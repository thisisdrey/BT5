## Analysis

The core broken invariant in the Sherlock report is: **a public, capital-funded action (deposit/mint) can push a shared capacity counter to its ceiling, causing a *different* legitimate public operation (swap/redeem/mint) to unconditionally revert, and the attacker can hold that state to their advantage while other actors are locked out.**

The closest local analog in this repository is `pallet-psm` (`substrate/frame/psm`), a Peg Stability Module pallet. It implements exactly this pattern with a **single, aggregate, shared debt ceiling** (`PsmInfo::max_debt`) that gates the public `mint` extrinsic for *all* approved external assets on a PSM instance, not just the specific asset being minted.

### The mechanism

In `mint()`, the aggregate check is:

```rust
let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
ensure!(
    current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
    Error::<T>::ExceedsMaxPsmDebt
);
``` [1](#0-0) 

This is checked *before* the per-asset ceiling check:

```rust
let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
let new_debt = current_debt.saturating_add(internal_equivalent);
ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);
``` [2](#0-1) 

Because `total_psm_debt` is the *sum across every approved external asset* on the instance, any single unprivileged account can mint up to `max_debt` using **one** external asset and thereby exhaust the shared ceiling for **all other externals simultaneously**. This is confirmed by the pallet's own test showing the aggregate ceiling blocks minting from a *different* external asset even though that asset's own per-asset allowance is untouched:

```rust
// Try to mint 50% + 1 via USDT (total would exceed PSM ceiling)
...
assert_noop!(
    Psm::mint(RuntimeOrigin::signed(BOB), INTERNAL_ASSET_ID, USDT_ASSET_ID, usdt_amount, Permill::from_percent(1)),
    Error::<Test>::ExceedsMaxPsmDebt
);
``` [3](#0-2) 

The attacker's minted position does not have to be redeemed quickly — `redeem()` only drains debt when the *holder* chooses to call it:

```rust
PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
    *debt = debt.saturating_sub(effective_internal_net);
});
``` [4](#0-3) 

So an attacker can mint up to `max_debt` (fully collateralized 1:1, so no direct capital loss beyond fees and opportunity cost — matching the report's "requires a large amount of capital" medium-severity framing) and simply **hold** the position, permanently pinning `total_psm_debt == max_debt`. Every other user's `mint()` call, regardless of which external asset they use, reverts with `ExceedsMaxPsmDebt` until the attacker redeems. Per the pallet's own documentation, this module exists specifically to arbitrage the internal stablecoin back to peg ("arbitrage opportunities exist when the internal asset trades outside $0.995-$1.005") [5](#0-4) , so blocking `mint()` system-wide directly disables the corrective mechanism the PSM is designed to provide, exactly analogous to how the TPDA report's attacker blocks liquidation bots from correcting vault state while the auction price decays.

### Why this is a real, low-privilege DOS

- No governance, admin, validator, relayer, or leaked-key assumption is required — any signed account can call `mint()`.
- The `ExceedsMaxPsmDebt` guard is exactly the mechanism meant to *protect* the system, but its aggregate (not per-asset) scope is what lets one account monopolize it.
- `set_max_debt` is a "mint-time throttle, not a hard invariant" per the docs [6](#0-5) , confirming there is no protection against an account parking debt right at the ceiling indefinitely.
- The circuit breaker (`set_asset_status`) and `set_max_debt` are Full/Emergency admin-only remedies [7](#0-6)  — i.e., the only fix requires privileged intervention, meaning normal users have no recourse once the ceiling is squatted.

### Title
Aggregate `max_debt` ceiling in `pallet-psm::mint` lets one unprivileged account DOS minting for all external assets on a PSM instance — (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::<T>::mint` enforces a single aggregate debt ceiling (`PsmInfo::max_debt`) shared across every approved external asset of a PSM instance before checking the per-asset ceiling. An attacker can mint fully-collateralized internal tokens using any one approved external asset up to `max_debt`, pin `total_psm_debt` at the ceiling, and simply not redeem — permanently reverting every other account's `mint()` call for every external asset on that instance until the attacker (or a privileged admin) chooses to unwind.

### Finding Description
`mint()` first checks the aggregate ceiling summed over all externals: [1](#0-0) 
then the per-asset ceiling: [2](#0-1) 

Because the first check is aggregate, filling it via one external asset denies minting through *every* external asset on the instance, even ones whose individual ceilings are nowhere near exhausted (as shown by the pallet's own `fails_mint_exceeds_aggregate_psm_ceiling` test) [8](#0-7) . Debt is only reduced when the debt-holder calls `redeem()`, which is entirely at the discretion of whoever holds the minted internal balance [4](#0-3) . There is no time-based decay, no per-account cap, and no automatic unwind — the ceiling can be squatted indefinitely by a single account.

### Impact Explanation
This blocks the PSM's core corrective function (arbitraging the internal stablecoin back to its peg) for every user and every approved external asset on the instance, not just the asset the attacker used. Given the pallet's stated arbitrage-band rationale [5](#0-4) , sustained denial of `mint()` can allow the internal asset to remain de-pegged for extended periods, and the only remedy is a privileged `set_max_debt`/circuit-breaker action by the instance's Full/Emergency admin — i.e., normal users have no recourse. This matches "public underpriced work that degrades... intended behavior" under the impact gate: an unprivileged, fully-collateralized action neutralizes a public safety mechanism for all other users.

### Likelihood Explanation
Requires only capital equal to `max_debt` (fully collateralized, no privileged role, no front-running needed — the attacker can simply mint and hold at any time, not just race a specific transaction), consistent with the "acknowledged, medium severity, requires large capital" characterization in the source report. Any PSM instance configured with multiple external assets sharing one aggregate ceiling is affected by design, not by misconfiguration.

### Recommendation
Either remove the aggregate `max_debt` check and rely solely on the sum of per-asset ceilings, or make the aggregate ceiling per-account/rate-limited/time-decaying so that one account cannot permanently occupy the full system-wide capacity. Alternatively, allow the redeem-side circuit breaker to be triggered automatically (not only by an admin) once the aggregate ceiling has been at its cap for longer than a configured duration, so that legitimate arbitrage/minting cannot be indefinitely denied by a single holder.

### Proof of Concept
1. PSM instance configured with `max_debt = D` and two approved externals, USDC (ceiling weight 50%) and USDT (ceiling weight 50%).
2. Attacker calls `mint(internal_asset, USDC_ASSET_ID, D, max_fee)` — succeeds since per-asset ceiling for USDC allows up to 50%*D... to reach the full aggregate, attacker can split across both externals up to their per-asset shares, summing to `D` total (`total_psm_debt == max_debt`), per the boundary test `boundary_new_debt_equals_max` [9](#0-8) .
3. Attacker holds the minted internal balance (does not call `redeem`).
4. Any other user calling `mint()` with USDC or USDT now reverts with `ExceedsMaxPsmDebt`, as demonstrated in `fails_mint_exceeds_aggregate_psm_ceiling` [8](#0-7) .
5. Minting on this PSM instance remains blocked for all externals until the attacker voluntarily redeems or a privileged admin raises `max_debt`.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L606-655)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// PSM doesn't have enough external asset for redemption.
		InsufficientReserve,
		/// Swap would exceed PSM debt ceiling.
		ExceedsMaxPsmDebt,
		/// Swap amount below the instance's minimum threshold.
		BelowMinimumSwap,
		/// Current fee exceeds the caller-provided maximum.
		FeeTooHigh,
		/// `create_psm` was called with a zero `min_swap_amount`.
		ZeroMinSwapAmount,
		/// Minting operations are disabled (circuit breaker level >= 1).
		MintingStopped,
		/// All swap operations are disabled (circuit breaker level = 2).
		AllSwapsStopped,
		/// Asset is not an approved external asset.
		UnsupportedAsset,
		/// No PSM instance is registered for the given internal asset.
		PsmNotFound,
		/// Asset is already in the approved list.
		AssetAlreadyApproved,
		/// Asset does not exist.
		AssetDoesNotExist,
		/// Cannot remove asset: not in approved list.
		AssetNotApproved,
		/// Cannot remove asset: has non-zero PSM debt.
		AssetHasDebt,
		/// Operation requires the instance's `full_admin` (Full level); the caller only
		/// matched the `emergency_admin` (Emergency level).
		InsufficientPrivilege,
		/// Maximum number of approved external assets reached.
		TooManyAssets,
		/// Live decimals diverged from the snapshot taken at registration or genesis.
		DecimalsMismatch,
		/// The asset's decimal precision is outside the supported range.
		DecimalsRangeExceeded,
		/// Decimal scaling produced an arithmetic overflow.
		ConversionOverflow,
		/// Conversion to the counter-asset rounds to zero; swap would transfer nothing.
		AmountTooSmallAfterConversion,
		/// A PSM is already registered for this internal asset.
		PsmAlreadyExists,
		/// The PSM has non-zero outstanding debt on at least one approved external.
		PsmHasDebt,
		/// The PSM still has approved externals; remove them before removing the PSM.
		PsmHasApprovedExternals,
		/// An unexpected invariant violation occurred. This should be reported.
		Unexpected,
	}
```

**File:** substrate/frame/psm/src/lib.rs (L732-736)
```rust
			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);
```

**File:** substrate/frame/psm/src/lib.rs (L738-741)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);
```

**File:** substrate/frame/psm/src/lib.rs (L889-891)
```rust
			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});
```

**File:** substrate/frame/psm/src/tests.rs (L295-317)
```rust
	#[test]
	fn boundary_new_debt_equals_max() {
		new_test_ext().execute_with(|| {
			// Set USDC to 100% and USDT to 0% so USDC gets full ceiling
			set_max_debt(200_000 * INTERNAL_UNIT);
			set_asset_ceiling_weight(USDC_ASSET_ID, Permill::from_percent(100));
			set_asset_ceiling_weight(USDT_ASSET_ID, Permill::from_percent(0));

			let max_debt = psm_max_asset_debt(USDC_ASSET_ID);

			fund_external_asset(USDC_ASSET_ID, ALICE, max_debt);

			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				max_debt,
				Permill::from_percent(1)
			));

			assert_eq!(PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID), max_debt);
		});
	}
```

**File:** substrate/frame/psm/src/tests.rs (L346-382)
```rust
	#[test]
	fn fails_mint_exceeds_aggregate_psm_ceiling() {
		new_test_ext().execute_with(|| {
			// Set both assets to 50% ratio each (100% total)
			// This tests that aggregate PSM ceiling is enforced even when per-asset ceilings allow
			set_asset_ceiling_weight(USDC_ASSET_ID, Permill::from_percent(50));
			set_asset_ceiling_weight(USDT_ASSET_ID, Permill::from_percent(50));

			let max_psm_debt = crate::Pallet::<Test>::max_psm_debt(&INTERNAL_ASSET_ID);

			// Mint 50% of PSM ceiling via USDC (succeeds)
			let usdc_amount = Permill::from_percent(50).mul_floor(max_psm_debt);
			fund_external_asset(USDC_ASSET_ID, ALICE, usdc_amount);
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				usdc_amount,
				Permill::from_percent(1)
			));

			// Try to mint 50% + 1 via USDT (total would exceed PSM ceiling)
			let usdt_amount = Permill::from_percent(50).mul_floor(max_psm_debt) + 1;
			fund_external_asset(USDT_ASSET_ID, BOB, usdt_amount);

			assert_noop!(
				Psm::mint(
					RuntimeOrigin::signed(BOB),
					INTERNAL_ASSET_ID,
					USDT_ASSET_ID,
					usdt_amount,
					Permill::from_percent(1)
				),
				Error::<Test>::ExceedsMaxPsmDebt
			);
		});
	}
```

**File:** substrate/frame/psm/README.md (L90-91)
```markdown
With 0.5% fees on both sides, arbitrage opportunities exist when the internal
asset trades outside $0.995-$1.005.
```

**File:** substrate/frame/psm/README.md (L142-144)
```markdown
Lowering a ceiling weight (or `max_debt`) below outstanding debt is allowed: the ceiling is a
mint-time throttle, so the external simply cannot be minted until redemptions bring its debt
back under the new ceiling.
```
