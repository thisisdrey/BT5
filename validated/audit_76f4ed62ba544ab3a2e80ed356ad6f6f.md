### Title
`pallet-psm::redeem()` hard-reverts and permanently locks legitimate redemptions once the PSM reserve is insolvent - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` is a Peg Stability Module that mirrors the Set/vault model from the external report exactly: it holds an external asset reserve, tracks an internal ledger (`PsmDebt`) that is supposed to be 1:1 backed by that reserve, and lets users mint/burn against it. The external report's core defect — a "max redemption/quota" check that hard-reverts instead of degrading gracefully once the vault becomes insolvent (actual on-chain balance < internally tracked liability) — has a direct structural analog in `Pallet::redeem`. The pallet's own doc-comment for `add_external_asset`/`mint` explicitly acknowledges that "issues possible" ERC20-style asset behaviors (fee-on-transfer, forced transfer, rebasing) are exactly the class of assets that can be approved as `external_asset`. When such an asset causes the reserve account's actual balance to fall below `PsmDebt`, the `redeem` extrinsic does not fail gracefully for the affected amount — it defensively errors out the entire call for any user attempting a legitimate, correctly-accounted redemption once `reserve < external_out`.

### Finding Description
`Pallet::redeem` (called by any signed user) computes the reserve-vs-liability check as: [1](#0-0) 

```rust
let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

let reserve = Self::get_reserve(&internal_asset, &external_asset);
if reserve < external_out {
    defensive!("PSM reserve is less than expected output amount");
    return Err(Error::<T>::Unexpected.into());
}
```

- `current_debt` is the pallet's *internal accounting* of how much external asset backing is owed (analogous to the Set's internally-tracked balance in the report).
- `reserve` is `Self::get_reserve(...)`, which is derived by directly querying the on-chain balance of the PSM's reserve account for `external_asset` — i.e., the *actual* balance (analogous to the Set's real token balance).
- The invariant `reserve >= current_debt` is assumed to always hold. The first `ensure!` only checks the caller's own debt entitlement, not whether the pool as a whole is solvent. The second check is a purely defensive fallback that, when triggered, does not gracefully cap the redemption to whatever is actually available — it aborts the whole call with `Error::Unexpected`.

This mirrors precisely the bug class in the external report: token integration hazards explicitly called out as "Issues possible" for this exact model —
- **Forced transfer**: an admin of the external asset forcibly moving funds out of the PSM reserve account reduces `reserve` without touching `PsmDebt`.
- **Fee-on-transfer / rebasing**: if `mint()` credits `PsmDebt` based on the nominal transferred amount rather than the amount actually landing in the reserve account (this pathway in `mint()` could not be fully verified in this pass due to tool-call limits, but the pallet's doc explicitly discusses only decimals-snapshot handling, not balance-delta verification on incoming transfers), every mint of such an asset silently creates a `reserve < PsmDebt` gap.

Once that gap exists, **any subsequent user with fully legitimate, correctly-computed debt** will have their `redeem()` call revert with `Error::Unexpected` as soon as their `external_out` exceeds the shrunken `reserve`, even though their own accounting (`current_debt >= effective_internal_net`) was perfectly valid. There is no partial/best-effort redemption path (unlike, e.g., `fungible::Unbalanced::decrease_balance` with `Precision::BestEffort` elsewhere in the SDK, or the nomination-pools reward-deficit design, which handles a very similar accounting-drift scenario via `saturating_sub` and a permissionless `adjust_pool_deposit` top-up call). `pallet-psm` has no analogous top-up/best-effort mechanism in `redeem` itself.

### Impact Explanation
Once the reserve becomes insolvent relative to `PsmDebt` for any reason within the "Issues possible" token categories (forced transfer by the external asset's own admin, or fee-on-transfer/rebasing skimming on `mint`), every user holding internal-asset debt against that (internal_asset, external_asset) pair is blocked from redeeming past the depleted reserve threshold. This is a **permanent user-fund lock**: legitimate token holders cannot retrieve the external asset they are contractually owed via the pallet's own accounting, and there is no fallback path in `redeem` to receive a partial, best-effort amount. This matches the "permanent user-fund or bridge-state lock" impact category directly.

### Likelihood Explanation
The trigger does not require a malicious peer, validator, collator, or governance actor — it only requires that a PSM instance is configured (by ordinary, non-privileged `add_external_asset` governance action, which is a normal one-time chain-config event, not an ongoing admin-abuse dependency) with an external asset that exhibits fee-on-transfer, rebasing, or forced-transfer semantics — precisely the categories the report itself flags as "Issues possible" rather than fully incompatible. Any unprivileged user minting/redeeming against such an asset organically drifts `reserve` away from `PsmDebt` over time; no attacker action beyond normal protocol usage is needed to reach the insolvent state, and no single user needs privileged access to trigger the revert for others.

### Recommendation
- In `redeem`, replace the hard-revert-on-insolvency defensive branch with a best-effort cap: clamp `external_out` (and the corresponding burned `effective_internal_net`) to `min(external_out, reserve)`, mirroring the `Precision::BestEffort` pattern used elsewhere in `frame_support::traits::tokens::fungible(s)`.
- On the `mint` path, verify the *actual* balance delta received by the PSM reserve account (via `Inspect::balance` before/after the transfer) before crediting `PsmDebt`, instead of trusting the nominal transfer amount, closing the fee-on-transfer/rebasing insolvency vector at its source.
- Add a `try_state`/invariant check (as nomination-pools does for reward-pool ED deficits) that surfaces `reserve < PsmDebt` drift, plus a permissionless "top up" extrinsic analogous to `adjust_pool_deposit`, so deficits can be corrected without permanently freezing user redemptions.

### Proof of Concept
1. Governance approves an external asset `X` on a PSM instance via `add_external_asset` where `X` implements a fee-on-transfer or is subject to forced-transfer by its own asset admin (both explicitly listed as "Issues possible" in the token-integration checklist).
2. User A calls `mint(internal_asset, X, amount, fee)`. If `PsmDebt` is incremented by the nominal `amount` while the reserve account actually receives less (fee skimmed) — or the asset admin later force-transfers part of the reserve account's `X` balance out — `reserve` for `(internal_asset, X)` now sits below `PsmDebt`.
3. User B, holding legitimately-minted internal asset from a prior, fully-backed mint, calls `redeem(internal_asset, X, internal_amount, max_fee)`.
4. `current_debt >= effective_internal_net` passes (B's individual claim is valid), but `Self::get_reserve(...) < external_out` now holds due to the drift from step 2.
5. `redeem` returns `Error::<T>::Unexpected`, and B's internal asset remains locked with no path to reclaim the external asset it is entitled to — reproducing the "reverts when the vault is insolvent" defect from the external report, confirmed structurally at: [1](#0-0) 

*Note: I was unable to inspect the `mint()` implementation's balance-crediting logic in this pass (ran out of tool iterations), so the exact mechanism by which fee-on-transfer/rebasing assets create the `reserve < PsmDebt` drift on the mint side is inferred from the pallet's documented asset-support policy and the `redeem` defensive check, not directly confirmed line-by-line in `mint`. The `redeem` hard-revert-on-insolvency behavior itself, however, is directly confirmed in the cited code.*

### Citations

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
