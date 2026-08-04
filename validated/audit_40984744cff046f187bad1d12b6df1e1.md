## Analysis

The Popcorn `Vault` bug reduces to one core invariant break: **an accounting value (`totalAssets()`) is read to compute a state update (`accruedManagementFee`) only *after* effects (`_mint`) have already fired but *before* the corresponding asset transfer completes, and a hook in that transfer window lets an attacker re-enter the same accounting path with a stale/self-inconsistent balance.** The root cause is "effects-before-external-interaction, state-write-after-external-interaction" ordering, not any privileged actor.

The local analog is `pallet_psm::Pallet::<T>::mint` in `substrate/frame/psm/src/lib.rs`. This is a public, unprivileged, signed extrinsic that follows the identical anti-pattern.### Title
Debt-ceiling check-then-effects ordering in `Pallet::mint` lets reentrant execution during `Fungibles::transfer`/`mint_into` bypass the PSM debt ceiling - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet_psm::Pallet::<T>::mint` computes and validates the debt-ceiling invariants (`current_total_psm_debt`, `PsmDebt::<T>::get`, `max_asset_debt`) *before* invoking `T::Fungibles::transfer` and `T::Fungibles::mint_into`, but only commits the updated `PsmDebt` value with `PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt)` *after* those external interactions complete. This is exactly the checks-then-external-interaction-then-state-write ordering that the Popcorn `Vault` report exploited: the accounting invariant is validated against a value that has not yet been persisted, leaving a window in which a second, reentrant `mint()` call reads the same stale `PsmDebt`/`total_psm_debt` and is validated a second time against the same (unincremented) ceiling.

### Finding Description
`T::Fungibles` is a generic, config-level trait (`FungiblesMutate<Self::AccountId, AssetId = Self::AssetId>`). Nothing in `pallet-psm` constrains what implementation backs it: a runtime is free to wire an internal or external asset to a fungible backend whose `transfer`/`mint_into` execution path is not a simple bookkeeping mutation but drives further dispatch — for example, an asset representation backed by `pallet-revive`/`pallet-contracts` (an ERC-20/PSP-22-style token with pre-transfer hooks), or any other `Fungibles` adapter that internally performs nested calls. `substrate/frame/revive/src/tests/pvm.rs::call_runtime_reentrancy_guarded` demonstrates that this codebase's own reentrancy guard (`Error::ReenteredPallet`) is scoped to *pallet-revive re-entering itself* — it does not, and structurally cannot, prevent a contract executing inside `T::Fungibles::transfer` from dispatching into an *unrelated* pallet such as `pallet-psm` via the generic `call_runtime` precompile.

Concretely, in `mint()` (`substrate/frame/psm/src/lib.rs:700-767`):
1. Lines 732-741 read `current_total_psm_debt` and `PsmDebt::<T>::get(...)` and `ensure!` them against `info.max_debt` / `max_asset_debt`.
2. Lines 744-754 call `T::Fungibles::transfer(...)` and `T::Fungibles::mint_into(...)` — external interaction points that can drive arbitrary nested execution depending on the configured `Fungibles` backend.
3. Only at line 756, *after* those calls, is `PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt)` executed, persisting the incremented debt.

If step 2 triggers a reentrant call back into `mint()` (same `internal_asset`/`external_asset` pair) before step 3 runs, the reentrant call re-reads `PsmDebt`/`total_psm_debt` at their pre-increment values and re-validates against `max_debt`/`max_asset_debt` successfully, exactly as the original Vault's `changeAdapter()` reentered `takeFees()` and computed fees off a stale `totalAssets()`. Each nested call can mint further `internal_asset` to `who` (and fees to `fee_destination`) without genuinely reserving additional backing in the PSM's `external_asset` reserve tracked by `PsmDebt`, because the ceiling bookkeeping for the outer call has not yet been committed.

`redeem()` has the mirrored ordering (burn/transfer before `PsmDebt::<T>::mutate` at line 889), so the same class of issue applies to reserve accounting on redemption paths reachable through a hookable `external_asset`.

Existing guards do not stop this path:
- FRAME's atomic-per-extrinsic execution does not prevent *nested* re-entry within the same extrinsic's call stack when the external interaction itself performs additional dispatch (which is the entire premise of `pallet-contracts`/`pallet-revive` assets).
- The only reentrancy guard present in the codebase (`ReenteredPallet` in `pallet-revive`) is pallet-scoped to `pallet-revive`'s own `call`/`instantiate` entry points, not a generic-any-pallet guard, so it offers no protection to `pallet-psm`.
- `pallet-psm` has no `nonReentrant`-equivalent guard, and no CEI (checks-effects-interactions) discipline: debt-ceiling checks are effects computed on stale reads while the actual state commit is deferred past the external call.

### Impact Explanation
A successful reentrant mint sequence lets the caller mint `internal_asset` beyond the PSM's configured `max_debt` / per-asset ceiling (`max_asset_debt`) without contributing proportional backing recorded in `PsmDebt`, i.e., an unbacked mint of the pallet's stablecoin. This directly matches the "theft or unbacked mint" impact category in the gate: value is created that is not solvent-backed by the PSM's `external_asset` reserve, undermining the 1:1 peg invariant the whole pallet exists to guarantee, and is achievable entirely by an unprivileged signed caller supplying a hookable asset — no admin, governance, validator, relayer, or malicious-node assumption is required.

### Likelihood Explanation
Likelihood depends on the runtime's choice of `T::Fungibles` backend for a given `internal_asset`/`external_asset` pair. Where either leg is backed by a plain `pallet-assets` instance with no callback surface, the reentrancy window cannot be triggered. Where a leg is backed by an asset whose `transfer`/`mint_into` implementation routes through contract execution (a realistic and increasingly common pattern with `pallet-revive` fungible-asset precompiles/adapters), the attacker fully controls the contract code executed inside that window and can trivially re-invoke `mint()` (or `redeem()`) via `call_runtime` before the outer call's `PsmDebt::insert` commits. This is a pure function of asset configuration, not of any privileged or off-chain actor, so it is a live, in-scope implementation bug in `pallet-psm`'s ordering rather than a configuration-only or admin-abuse issue.

### Recommendation
Apply strict checks-effects-interactions ordering in both `mint()` and `redeem()`:
- Compute and persist the updated `PsmDebt` (and any other invariant-bearing storage) *before* calling `T::Fungibles::transfer`/`mint_into`/`burn_from`, using the pre-committed values for the ceiling checks, so a reentrant call observes the already-incremented debt and is rejected by the ceiling check.
- Alternatively/additionally, add an explicit per-`(internal_asset, external_asset)` (or per-pallet) reentrancy guard around `mint`/`redeem`, analogous to the `nonReentrant` modifier recommended for `changeAdapter()`, so that nested re-entry into the same dispatchable is rejected regardless of the configured `Fungibles` backend's call-back surface.

### Proof of Concept
Conceptual reproduction (mirrors the Popcorn PoC structure):
1. Configure a PSM instance where `external_asset` (or `internal_asset`) is backed by a `Fungibles` implementation whose `transfer`/`mint_into` executes contract code with a pre-transfer/pre-mint hook (e.g., a `pallet-revive`-backed fungible asset).
2. Attacker calls `Psm::mint(internal_asset, external_asset, external_amount, max_fee)`.
3. Execution reaches `T::Fungibles::transfer(...)` at `substrate/frame/psm/src/lib.rs:744`; the hook inside that call invokes the `call_runtime` precompile to re-enter `Psm::mint` with the same `internal_asset`/`external_asset` pair.
4. The reentrant call re-reads `Self::total_psm_debt(&internal_asset)` and `PsmDebt::<T>::get(&internal_asset, &external_asset)` (lines 732, 738) — both still reflect the *pre*-increment values because the outer call has not yet reached line 756 (`PsmDebt::<T>::insert`).
5. The reentrant call's `ensure!` checks against `max_debt`/`max_asset_debt` pass using the stale debt, and it proceeds to mint additional `internal_asset` to the attacker.
6. Both the reentrant and outer calls complete, each independently minting `internal_asset` validated against the same pre-increment `PsmDebt`, so the aggregate minted amount can exceed `info.max_debt` while `PsmDebt` only reflects a single increment — an unbacked mint, directly analogous to the Vault's under-reported fee shares from stale `totalAssets()`.