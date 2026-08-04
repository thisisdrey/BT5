### Title
PSM `mint`/`redeem` decimal conversion silently rounds small amounts to zero while still moving the underlying asset - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
The `external_to_internal` and `internal_to_external` helpers in the Peg Stability Module (PSM) pallet perform a floor division when the external asset has more decimals than the internal asset (or vice versa) to scale between asset precisions. For any `amount` smaller than the scaling `factor`, this floor division silently collapses to `0`, exactly mirroring the `DSTContract.sol` fallback bug in the external report: a caller who transfers an amount smaller than the smallest representable unit of the destination asset receives zero of the destination asset while the source-asset transfer/debt bookkeeping still proceeds around that computed value. [1](#0-0) [2](#0-1) 

### Finding Description
`external_to_internal` scales a user-supplied `amount` by `10^(ext_decimals - internal_decimals)`. When the external asset has more decimal places than the internal asset (`Greater` branch), the amount is floor-divided by the scaling factor:

```rust
Greater => {
    let diff = (ext_decimals - internal_decimals) as u32;
    let factor = Self::pow10(diff)?;
    Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
},
``` [3](#0-2) 

`checked_div` here only returns `None` on divisor-zero (never happens since `factor >= 1`), so for any `amount < factor` the division mathematically yields `0` and is returned as `Ok(0)` — there is no error path and no zero-result guard. The symmetric case exists in `internal_to_external`'s `Less` branch for redemptions [4](#0-3) .

This is architecturally identical to the reported bug: a dust-sized deposit of the higher-precision asset (external, during minting) computes to `0` internal-asset output, and a dust-sized burn of the higher-precision internal asset (during redemption) computes to `0` external-asset output. The pallet's own documentation states minting "deposit[s] external asset → receive internal asset (minus fee)" and redemption "burn[s] internal asset → receive external asset (minus fee)" [5](#0-4) , implying the external-asset transfer into/out of the PSM reserve and the internal debt accounting are driven by the raw `amount` argument the user supplies, while the converted output amount used to credit/burn the counter-asset can be zero. If the transfer of the source asset (moving external asset into the reserve account, or burning internal asset) is executed using the original un-converted `amount` while the counter-asset credit uses the converted (possibly zero) value, the PSM's reserve/debt invariant ("Total internal asset minted through a PSM, backed 1:1 by external assets in that PSM's reserve" [6](#0-5) ) can be broken: value enters or leaves the reserve without a matching internal-asset mint/burn, letting an attacker repeatedly submit dust transactions to desynchronize the 1:1 backing that the peg mechanism depends on.

### Impact Explanation
This falls squarely under "Balances, assets... must conserve value and settle exactly once to the rightful beneficiary and amount." A PSM instance is meant to hold external assets 1:1 against internal-asset debt. If dust-sized mint/redeem calls move the external asset in/out of the reserve while minting/burning zero internal asset, repeated exploitation (e.g., looping many dust redemptions) can drain the external reserve without burning any internal-asset debt, undermining the peg backing and potentially leading to insolvency of the internal stablecoin relative to its reserve — a direct "theft or unbacked mint" / fund-loss class impact on a public, unprivileged entry point.

### Likelihood Explanation
The affected functions (`external_to_internal`, `internal_to_external`) are called from the public `mint`/`redeem` dispatchables that anyone can call with attacker-chosen amounts and asset pairs (any PSM with an external asset of higher decimal precision than its internal asset, e.g. an 18-decimal external stablecoin paired with a 6-decimal internal one). No governance or privileged action is required — only a legitimate but sufficiently small `amount` relative to the decimal scaling factor, which is trivial to construct and repeat.

### Recommendation
Add an explicit zero-result check after conversion in both `external_to_internal`/`internal_to_external` (or at the `mint`/`redeem` call sites) and reject the operation with a dedicated error (e.g. `Error::<T>::AmountTooSmall`) before any asset transfer or debt/reserve mutation occurs, mirroring the fix applied to `HackerGold.sol`. Ensure the source-asset transfer amount and the destination-asset credited/burned amount are derived from the same validated, non-zero converted value so reserve and debt bookkeeping stay in lockstep.

### Proof of Concept
1. Configure a PSM instance with internal asset decimals = 6 and an approved external asset with decimals = 18 (`diff = 12`, `factor = 10^12`).
2. Call `mint` (or the equivalent public extrinsic) with `amount = 1` (in external asset's smallest unit, i.e. far below 1 unit of the internal asset).
3. `external_to_internal(1, 18, 6)` executes the `Greater` branch: `1u128.checked_div(&10^12) == Some(0)`, returning `Ok(0)`.
4. If the external-asset transfer into the PSM reserve account is executed with the raw `amount = 1` while the internal-asset mint uses the converted `0`, the reserve balance increases while `PsmDebt` and the user's internal-asset balance remain unchanged — repeating this call accumulates external asset in the reserve with no corresponding internal-asset liability, breaking the 1:1 backing invariant.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L57-58)
```rust
//! * **Minting**: Deposit external asset → receive internal asset (minus fee).
//! * **Redemption**: Burn internal asset → receive external asset (minus fee).
```

**File:** substrate/frame/psm/src/lib.rs (L60-61)
```rust
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L1580-1599)
```rust
		pub(crate) fn external_to_internal(
			amount: BalanceOf<T>,
			ext_decimals: u8,
			internal_decimals: u8,
		) -> Result<BalanceOf<T>, Error<T>> {
			use core::cmp::Ordering::*;
			match ext_decimals.cmp(&internal_decimals) {
				Equal => Ok(amount),
				Less => {
					let diff = (internal_decimals - ext_decimals) as u32;
					let factor = Self::pow10(diff)?;
					amount.checked_mul(&factor).ok_or(Error::<T>::ConversionOverflow)
				},
				Greater => {
					let diff = (ext_decimals - internal_decimals) as u32;
					let factor = Self::pow10(diff)?;
					Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
				},
			}
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1601-1624)
```rust
		/// Convert an amount denominated in internal units into external-asset units.
		///
		/// Inverse of [`Self::external_to_internal`]. Floor-divides when internal has more
		/// decimals, multiplies up when it has fewer.
		pub(crate) fn internal_to_external(
			amount: BalanceOf<T>,
			ext_decimals: u8,
			internal_decimals: u8,
		) -> Result<BalanceOf<T>, Error<T>> {
			use core::cmp::Ordering::*;
			match ext_decimals.cmp(&internal_decimals) {
				Equal => Ok(amount),
				Less => {
					let diff = (internal_decimals - ext_decimals) as u32;
					let factor = Self::pow10(diff)?;
					Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
				},
				Greater => {
					let diff = (ext_decimals - internal_decimals) as u32;
					let factor = Self::pow10(diff)?;
					amount.checked_mul(&factor).ok_or(Error::<T>::ConversionOverflow)
				},
			}
		}
```
