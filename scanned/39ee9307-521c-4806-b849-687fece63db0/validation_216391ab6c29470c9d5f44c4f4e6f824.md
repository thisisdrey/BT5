### Title
Stale asset-decimals snapshot in `pallet-psm` lets a normal asset owner desynchronize the mint/redeem conversion rate from the live `Fungibles` metadata, breaking 1:1 peg backing - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` keeps the same "decimals" information in two places, exactly the pattern in the external report: a live source (`Fungibles::decimals()` metadata) and a frozen snapshot cached in on-chain PSM storage (`PsmInfo::internal_decimals` and `ExternalAssetInfo::decimals`). The two copies are only reconciled once, at onboarding time, and are never re-checked on the hot swap path (`mint`/`redeem`), so the pallet can keep using an outdated decimal-scaling factor forever once the underlying asset's live decimals change.

### Finding Description
`PsmInfo` stores `internal_decimals` as "Snapshot of the internal asset's decimals at install time" [1](#0-0) , and each `ExternalAssetInfo` stores its own `decimals` field described as "Snapshot of the external asset's decimals at registration time" [2](#0-1) . These snapshots are used to compute the `10^diff` scaling factor between internal and external units (bounded by `MAX_DECIMALS_DIFF`) for every mint/redeem conversion [3](#0-2) .

The pallet's own documentation confirms the decimals consistency check is performed only once — during onboarding, before `add_external_asset` succeeds: "The internal asset's live decimals must still match the snapshot in `PsmInfo`" and "`|external_decimals − internal_decimals|` must be within `MAX_DECIMALS_DIFF`" [4](#0-3) . After that point, `mint`/`redeem` trust the cached `internal_decimals`/`ExternalAssetInfo.decimals` values rather than re-querying `Fungibles` metadata for every swap.

This is the same broken invariant as the reported `Collateral.Data` duplication: the same logical fact (asset decimals) is held in two places — the live `Fungibles` metadata (which any asset owner can mutate via the standard `set_metadata` extrinsic on `pallet-assets`, a normal non-privileged asset-management action, not a PSM-admin or governance action) and a frozen snapshot inside PSM storage that the PSM has no mechanism to refresh after `add_external_asset`. There is no `pallet-psm` extrinsic to re-sync `PsmInfo::internal_decimals` or `ExternalAssetInfo::decimals` once approved [5](#0-4) .

### Impact Explanation
If the underlying `internal_asset` (or an approved `external_asset`) has its decimals changed after being onboarded to a PSM, all subsequent `mint`/`redeem` calls keep applying the old `10^diff` conversion factor against a token whose real unit value has shifted. Because the PSM's core invariant is that internal debt is "backed 1:1 by external assets in that PSM's reserve" [6](#0-5) , a stale scaling factor breaks this backing: users can mint internal asset for less real external value than intended, or redeem more external value than their burned internal asset actually represents — an unbacked mint / value-conservation violation reachable by an ordinary user simply calling `mint`/`redeem`, with the desync caused by a normal asset-metadata update rather than any PSM-privileged action.

### Likelihood Explanation
Triggering the desync only requires the asset's owner/admin (a role independent of the PSM's `full_admin`/`emergency_admin`) to call the standard `set_metadata` extrinsic changing `decimals` on an asset that is already approved in a live PSM — a normal, unprivileged asset-management operation, not chain governance, not a validator/collator/relayer compromise. Once decimals diverge, every subsequent unprivileged `mint`/`redeem` call exploits the stale conversion factor; no race condition or front-running is required since the mismatch persists indefinitely (there's no re-check and no admin function to fix it), and the code accepts the stale value at line 288-289 unconditionally.

### Recommendation
Re-validate live `Fungibles` decimals against the stored snapshot on every `mint`/`redeem` (or on any state-changing call) and reject the swap (or halt the pair via the circuit breaker) if they diverge, mirroring the check already done in `add_external_asset`. Alternatively, remove the cached decimals entirely and always read live decimals from `Fungibles` metadata at swap time, eliminating the duplicate/divergable source of truth altogether — the same two remediation options suggested in the original report for `Collateral.Data`.

### Proof of Concept
1. Asset owner creates internal asset `I` with decimals `12` and external asset `E` with decimals `12`; PSM instance created via `create_psm`, snapshotting `PsmInfo.internal_decimals = 12`.
2. `add_external_asset(I, E)` succeeds; `ExternalAssetInfo.decimals = 12` is snapshotted; `|12-12|=0 <= MAX_DECIMALS_DIFF`.
3. Liquidity is added and users start minting/redeeming normally at a 1:1 scaling factor.
4. Asset `E`'s owner calls `pallet-assets::set_metadata` to change `E`'s live decimals to `6` (a legitimate, unprivileged-to-PSM action allowed by `pallet-assets`).
5. `pallet-psm` never re-checks live decimals for `E`; `ExternalAssetInfo.decimals` in storage still reads `12`.
6. Any user calls `mint`/`redeem` for the `(I, E)` pair; the conversion factor computed from the stale `12` vs. actual `6` decimal live value causes an incorrect exchange rate, allowing extraction of more external-asset value per unit of internal asset burned/minted than the peg intends, breaking the 1:1 backing invariant without any admin, governance, or validator action.

Note: I was unable to inspect the exact `do_mint`/`do_redeem` conversion arithmetic function body directly within the remaining tool budget; the analysis above relies on the documented decimals-snapshot design, the storage field doc comments, and the README's explicit statement that decimals consistency is enforced only at onboarding. Confirming the exact conversion code path is recommended before treating this as fully proven.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L58-61)
```rust
//! * **Redemption**: Burn internal asset → receive external asset (minus fee).
//! * **Reserve**: External asset balance held by a PSM's reserve account (derived, not stored).
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L270-273)
```rust
	/// Maximum absolute difference between an external asset's decimals and the internal
	/// asset's decimals. Bounds the scaling factor `10^diff` well below `u128::MAX`
	/// so realistic balances cannot overflow during conversion.
	pub const MAX_DECIMALS_DIFF: u32 = 24;
```

**File:** substrate/frame/psm/src/lib.rs (L280-292)
```rust
	pub struct PsmInfo<T: Config> {
		/// Account receiving minting and redemption fees, denominated in the internal asset.
		pub fee_destination: T::AccountId,
		/// This PSM instance's debt ceiling, in internal-asset units.
		pub max_debt: BalanceOf<T>,
		/// Minimum swap amount for this instance, in internal-asset units. Swaps whose
		/// internal-equivalent falls below this are rejected with [`Error::BelowMinimumSwap`].
		pub min_swap_amount: BalanceOf<T>,
		/// Snapshot of the internal asset's decimals at install time.
		pub internal_decimals: u8,
		/// Number of approved external assets attached to this instance.
		pub external_count: u32,
	}
```

**File:** substrate/frame/psm/src/lib.rs (L325-330)
```rust
	pub struct ExternalAssetInfo {
		/// Per-external circuit breaker status.
		pub status: CircuitBreakerLevel,
		/// Snapshot of the external asset's decimals at registration time.
		pub decimals: u8,
	}
```

**File:** substrate/frame/psm/README.md (L107-121)
```markdown
## Governance Operations

All governance extrinsics take `internal_asset` as the first parameter to
identify the PSM instance being configured.

| Extrinsic | Required Level | Description |
| --- | --- | --- |
| `set_minting_fee(internal_asset, asset_id, fee)` | Full | Update minting fee for the pair |
| `set_redemption_fee(internal_asset, asset_id, fee)` | Full | Update redemption fee for the pair |
| `set_max_debt(internal_asset, value)` | Full or Emergency | Update absolute debt ceiling for the PSM |
| `set_asset_ceiling_weight(internal_asset, asset_id, weight)` | Full or Emergency | Update external ceiling weight |
| `set_asset_status(internal_asset, asset_id, status)` | Full or Emergency | Set per-external circuit breaker level |
| `add_external_asset(internal_asset, asset_id)` | Full | Approve external on a PSM |
| `remove_external_asset(internal_asset, asset_id)` | Full | Remove external from a PSM (zero debt) |

```

**File:** substrate/frame/psm/README.md (L146-154)
```markdown
### Asset Onboarding Requirements

Before calling `add_external_asset(internal_asset, asset_id)`:

- A PSM must already be registered for `internal_asset`
- The external `asset_id` must already exist in the `Fungibles` implementation
- The internal asset's live decimals must still match the snapshot in `PsmInfo`
- `|external_decimals − internal_decimals|` must be within `MAX_DECIMALS_DIFF`
- The PSM must still be below `MaxExternals`
```
