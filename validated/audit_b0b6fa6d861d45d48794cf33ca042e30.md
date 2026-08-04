### Title
Reentrancy-style check bypass: PSM debt ceiling updated only after external asset transfer, allowing repeated mint beyond `max_debt` - (File: substrate/frame/psm/src/lib.rs)

### Summary
The `pallet-psm` `mint` dispatchable reads and checks the debt-ceiling (`PsmDebt`, `max_debt`) *before* performing the external-asset pull (`T::Fungibles::transfer`) and only writes the updated `PsmDebt` value *after* the mint of internal tokens completes. This is the same "check funds availability → pull external funds → do work → push funds/update accounting last" ordering that the reported Exchange contract used, which enabled reentrant draining via `recoverLostFunds`. If `T::Fungibles::transfer` (or any downstream hook it triggers on the external asset, e.g. an asset implemented/backed by a contract via `pallet-revive`) can call back into the runtime before it returns, an attacker can re-enter `mint` and pass the stale (pre-update) debt-ceiling check repeatedly, minting internal stablecoin far beyond the configured `max_debt`/`PsmInfo::max_debt` ceiling.

### Finding Description
In `substrate/frame/psm/src/lib.rs`, `Pallet::mint`:
1. Reads `current_total_psm_debt` and per-asset `current_debt`/`max_debt` and enforces `Error::ExceedsMaxPsmDebt` using the value currently in storage.
2. Only afterwards calls `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, ...)` — the external-value-pulling step.
3. Then calls `T::Fungibles::mint_into(internal_asset, &who, internal_to_user)` and optionally mints the fee.
4. Only at the very end calls `PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt)` to persist the updated debt.

This exactly mirrors the reported anti-pattern: pull funds from the user, do work, and update the guarding accounting state only at the end. Any reentrant call into `mint` (or `redeem`) that occurs during steps 2–3 — for instance if `external_asset` is a foreign/contract-backed asset whose transfer implementation can execute external logic before returning (a scenario materially enabled in this codebase by `pallet-revive`'s ERC20/asset precompiles and EVM contract execution model, where a "asset" can be implemented via arbitrary Solidity/PVM code with transfer-time callbacks) — will see the stale `PsmDebt`/`current_total_psm_debt` values and pass the ceiling check again, even though a prior in-flight call has already consumed capacity that has not yet been written back to storage. Because Substrate dispatch is normally single-threaded and non-reentrant *unless* an external call is made mid-execution, this class of bug specifically requires a callback path — which `pallet-revive`'s configurable `Fungibles`/asset backends can provide when assets are not the native `pallet-assets` fungible but a contract-controlled implementation.

The `redeem` function has a similar but less severe ordering issue: `PsmDebt::mutate` (debt decrease) happens after the external transfer to `who`, but redeem burns internal tokens from the caller first (reducing what the caller can re-spend), so the primary attack surface is `mint`, where value is created (minted) based on a check that is not yet reflected in storage.

### Impact Explanation
This directly matches the required impact category "theft or unbacked mint": a successful reentrant sequence lets an attacker mint internal stablecoin units backed by insufficient external collateral, exceeding `PsmInfo::max_debt` and the per-external ceiling (`max_asset_debt`). This breaks the core invariant that the PSM's issued internal-asset supply is always fully backed by its external reserve, and can leave the pallet under-collateralized/insolvent — a chain-level runtime accounting bug, not one requiring a malicious validator, governance actor, or leaked key.

### Likelihood Explanation
Exploitability is conditioned on `T::Fungibles`/the specific `external_asset` implementation allowing a callback during `transfer` before returning control to `mint`. In a deployment where `external_asset` is routed through `pallet-revive`-backed fungibles (contracts implementing token semantics, as the repo's `ERC20`/asset precompiles demonstrate is a supported configuration), this is plausible and requires only an unprivileged user controlling/depositing a custom asset contract registered as an approved external — no relayer, validator, or admin collusion needed, only that `add_external_asset` accepted a caller-influenced asset. Because the codebase's own reentrancy documentation (`pallet-revive`'s `ReentrancyProtection`) shows this project actively guards against exactly this callback-based reentry pattern elsewhere, but the PSM pallet's `mint`/`redeem` logic performs no reentrancy guard and defers the debt-ceiling write to the end of the call, the vulnerability is a straightforward violation of the check-effects-interactions pattern.

### Recommendation
- Reorder `mint`/`redeem` to update `PsmDebt` (and any other ceiling-guarding storage) *before* performing the external asset transfer/pull, or wrap the debt update and the external transfer in a way that the debt state reflects the reservation atomically with the check (e.g., increment-then-transfer-then-rollback-on-failure).
- Add an explicit reentrancy guard (a per-pallet or per-instance "in progress" flag/`StorageValue<bool>`) around `mint`/`redeem` so a nested call to the same dispatchables fails fast, similar to `pallet-revive`'s `ReentrancyProtection::Strict`.
- Audit which asset backends can be configured as `T::Fungibles`/approved externals to ensure none can execute arbitrary code during `transfer`, or explicitly document/restrict `add_external_asset` to non-callback-capable asset classes only.

### Proof of Concept
Conceptual sequence (assuming an external asset backend that can trigger a callback during `T::Fungibles::transfer`):
1. Attacker registers/uses an external asset `E` (approved via `add_external_asset`) whose `transfer` implementation, when called with `psm_account` as destination, executes attacker-controlled code before returning (e.g., a contract-backed asset routed through `pallet-revive`).
2. Attacker calls `psm::mint(internal, E, external_amount, max_fee)`.
3. Execution reaches step 2 above: `T::Fungibles::transfer(E, attacker, psm_account, effective_external, ...)`. Before this call returns, attacker's callback re-enters `psm::mint(internal, E, external_amount, max_fee)`.
4. The reentrant call reads `PsmDebt`/`current_total_psm_debt` — still showing the pre-first-call value, since `PsmDebt::<T>::insert` for the first call hasn't executed yet — and passes the `ExceedsMaxPsmDebt` check again.
5. This can be repeated (bounded by available external liquidity supplied by attacker per call, but each nested completion mints `internal_to_user` on top of `max_debt`), producing minted internal-asset supply that exceeds `PsmInfo::max_debt`. [1](#0-0) [2](#0-1)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L732-756)
```rust
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

**File:** substrate/frame/psm/src/lib.rs (L848-891)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}

			if !fee.is_zero() {
				T::Fungibles::transfer(
					internal_asset.clone(),
					&who,
					&info.fee_destination,
					fee,
					Preservation::Expendable,
				)?;
			}

			if !effective_internal_net.is_zero() {
				T::Fungibles::burn_from(
					internal_asset.clone(),
					&who,
					effective_internal_net,
					Preservation::Expendable,
					Precision::Exact,
					Fortitude::Polite,
				)?;
			}

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

			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});
```
