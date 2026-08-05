### Title
PSM `mint`/`redeem` credit internal-asset amount computed from the nominal transfer amount instead of the external asset's actual balance delta, allowing unbacked internal-asset debt for any fee-charging `T::Fungibles` implementation - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s `mint()` computes how much internal asset to issue (`internal_equivalent`, and thus `internal_to_user` + `fee`) purely from the caller-specified `external_amount`, then separately calls `T::Fungibles::transfer(...)` to move `effective_external` into the PSM reserve account. There is no balance-before/after check confirming that the reserve account actually received `effective_external`. If the concrete `T::Fungibles` implementation used for a given `external_asset` ever deducts anything on transfer (a transfer tax/fee, a burn-on-transfer mechanic, or any non-1:1 wrapped/foreign asset semantics), the PSM will mint internal asset and record `PsmDebt` as if the full nominal amount arrived, while the reserve actually holds less. This is the exact bug class described in the wfCash report: shares/claims issued 1:1 against a nominal deposit amount that does not match the actual asset actually credited to the vault/pool account.

### Finding Description
In `mint()` (`substrate/frame/psm/src/lib.rs`, `Pallet::mint`): [1](#0-0) 

The flow is:
1. `internal_equivalent` is derived purely from the caller-supplied `external_amount` via `external_to_internal` (a fixed decimal-scaling conversion, not tied to any actual transfer result).
2. `internal_to_user` and `fee` are computed from `internal_equivalent` alone.
3. `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, Preservation::Expendable)` is called and only its `Result<_, DispatchError>` is checked (success/failure) — the actual amount that landed in `psm_account` is never read back or compared to `effective_external`.
4. `T::Fungibles::mint_into(internal_asset, &who, internal_to_user)` and `PsmDebt::insert(..., new_debt)` then unconditionally credit/track the full nominal amount.

This mirrors the wfCash flaw precisely: Notional's `TokenHandler` credits the vault prime cash based on **actual** balance-before/after transfer amounts (accounting correctly for fee-on-transfer), but `wfCashLogic` mints shares based on the **nominal** deposit amount — creating a permanent gap between claims outstanding and backing assets held. In `pallet-psm`, the same asymmetry exists: `PsmDebt` (the internal-asset debt, which is supposed to be "backed 1:1 by external assets in that PSM's reserve" per the pallet's own documentation) is incremented by the nominal `internal_equivalent`, while the actual external reserve balance increase depends entirely on how `T::Fungibles::transfer` behaves for that specific asset id. `T::Fungibles` is a generic, pluggable trait bound (implemented in the current runtimes by `pallet-assets`/fungibles adaptors), so any asset type registered as a PSM external whose transfer semantics are not a strict 1:1 debit/credit (a taxed/foreign/ERC20-wrapped asset exposed through a `fungibles::Mutate` adaptor, e.g., through the ERC20-transactor style bridging shown elsewhere in the codebase) breaks the reserve-1:1 invariant silently, with no defensive check catching it at mint time.

Once this occurs, the PSM immediately becomes under-collateralized: `PsmDebt` (and hence the mintable internal-asset supply for that pair) exceeds the actual reserve balance. The `redeem()` path only detects this after the fact, via `if reserve < external_out { defensive!(...); return Err(Error::<T>::Unexpected) }`, i.e. failure occurs for later redeemers, not the depositor who caused the shortfall — reproducing the wfCash "last depositor cannot withdraw" pattern: [2](#0-1) 

### Impact Explanation
This breaks the "conserve value and settle exactly once" invariant for asset accounting explicitly called out in scope: internal-asset (stablecoin) debt is recorded and mintable against a reserve that does not actually hold the corresponding external assets. Effect:
- Internal asset issued via `mint()` becomes unbacked (partially uncollateralized stablecoin supply) for any external asset whose `T::Fungibles::transfer` deducts a fee/tax.
- The PSM reserve account permanently lacks funds to redeem all internal-asset debt at that pair, meaning some redeemers hit `Error::Unexpected`/`InsufficientReserve` and their internal-asset holdings become effectively unredeemable/locked — a permanent user-fund lock on a first-come-first-served basis.
- Because minting fees are computed from the same corrupted nominal `internal_equivalent`, more internal asset can continue to be minted against the same insufficient reserve on every subsequent deposit, compounding the shortfall.

### Likelihood Explanation
Exploitability does not require any privileged actor, malicious relayer, or governance abuse — it is purely a function of which asset a governance/admin registers via `add_external_asset` and which concrete implementation is wired to `T::Fungibles` in a given runtime deployment. Any unprivileged user depositing that asset via the public `mint` extrinsic triggers the shortfall automatically; the reporter (or any depositor) does not need to do anything special beyond calling `mint()` with a fee-charging external asset already listed by the PSM. The severity in the analogous wfCash finding was assessed Medium (not High) specifically because it requires a fee-charging token to be configured as backing, which is the same qualifying condition here — but once that condition holds, exploitation is automatic and needs zero attacker sophistication.

### Recommendation
1. In `mint()`, use a balance-before/after check on `psm_account` around the `T::Fungibles::transfer` call (mirroring Notional's `TokenHandler::_postTransferPrimeCashUpdate` fix pattern) and derive `internal_equivalent`/`internal_to_user` from the **actual** amount credited to the reserve, not from the caller-supplied `external_amount`.
2. Alternatively/additionally, add an explicit `is_fee_on_transfer`-style flag or an assertion in `add_external_asset` that only accepts external assets whose transfer semantics are provably 1:1 (e.g. restrict to `T::Fungibles` implementations that guarantee no transfer-time deduction), and reject/flag any others.
3. In `redeem()`, apply the same defensive symmetry: verify actual balance decrease of `psm_account` matches `external_out` before decrementing `PsmDebt`.

### Proof of Concept
1. Governance registers an `external_asset` on some `internal_asset` PSM instance where `T::Fungibles::transfer` is backed by an asset implementation that deducts a 2% fee on every transfer (e.g. a foreign/wrapped asset bridged through a custom `fungibles::Mutate` adaptor).
2. Alice calls `Psm::mint(internal_asset, external_asset, 1_000 * UNIT, Permill::zero())`.
   - `internal_equivalent` is computed as `1_000 * UNIT` worth of internal asset (scaled for decimals).
   - `T::Fungibles::transfer(external_asset, alice, psm_account, effective_external, Expendable)` executes, but due to the 2% fee only `980 * UNIT`-equivalent actually lands in `psm_account`.
   - `mint_into(internal_asset, alice, internal_to_user)` still mints ~`1_000 * UNIT` (minus PSM's own configured fee) to Alice, and `PsmDebt` is increased by the full `internal_equivalent`.
3. Reserve now holds `980` external-asset units but `PsmDebt` (and outstanding internal-asset claims) reflects `1_000` units of backing.
4. When holders collectively attempt to `redeem()` the full outstanding internal-asset supply for that pair, the later redeemers hit `reserve < external_out` → `defensive!("PSM reserve is less than expected output amount")` → `Error::<T>::Unexpected`, permanently unable to redeem their internal-asset balance for that external asset.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L719-754)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L848-855)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}
```
