## Title
Emergency Admin Cannot Lower a PSM's Debt Ceiling or Asset Ceiling Weight, Contradicting Documented Privilege Model - (File: `substrate/frame/psm/src/lib.rs`)

## Summary
`pallet-psm`'s own README documents that `set_max_debt` and `set_asset_ceiling_weight` are callable at **"Full or Emergency"** privilege level, i.e., that the `emergency_admin` origin should be able to invoke them for rapid crisis response [1](#0-0) . However, the actual authorization logic in `PsmManagerLevel` restricts both operations to `Full` only, silently omitting `Emergency` [2](#0-1) . This is the same bug class as the external `DelayedAdmin` report: a documented, security-relevant emergency capability that the responsible privileged actor is never actually wired to invoke.

## Finding Description
The pallet defines two admin levels per PSM instance, `Full` (held by `full_admin`) and `Emergency` (held by `emergency_admin`) [3](#0-2) . The `PsmManagerLevel` helper methods gate each governance extrinsic:

```rust
/// Whether this level allows modifying the circuit breaker status.
/// Both Full and Emergency levels can set circuit breaker.
pub const fn can_set_circuit_breaker(&self) -> bool {
    matches!(self, PsmManagerLevel::Full | PsmManagerLevel::Emergency)
}

/// Whether this level allows modifying the PSM debt ceiling.
/// Only Full can set the debt ceiling.
pub const fn can_set_max_debt(&self) -> bool {
    matches!(self, PsmManagerLevel::Full)
}

/// Whether this level allows modifying per-asset ceiling weights.
/// Only Full can set asset ceiling weights.
pub const fn can_set_asset_ceiling(&self) -> bool {
    matches!(self, PsmManagerLevel::Full)
}
``` [4](#0-3) 

The pallet's own error variant confirms this restriction is enforced in the call path: "Operation requires the instance's `full_admin` (Full level); the caller only matched the `emergency_admin` (Emergency level)" [5](#0-4) .

Yet the README's governance table explicitly states both `set_max_debt` and `set_asset_ceiling_weight` require only "Full or Emergency" — the same level as `set_asset_status` (the circuit breaker), which *is* correctly wired to both admins:

```
| `set_max_debt(internal_asset, value)` | Full or Emergency | Update absolute debt ceiling for the PSM |
| `set_asset_ceiling_weight(internal_asset, asset_id, weight)` | Full or Emergency | Update external ceiling weight |
| `set_asset_status(internal_asset, asset_id, status)` | Full or Emergency | Set per-external circuit breaker level |
``` [6](#0-5) 

This is a direct structural analog to the `DelayedAdmin`/`PauseAdmin` bug: a two-tier admin design where the documentation specifies the lower-privilege ("emergency") actor should have a specific fast, surgical remediation lever, but the code implementing the privilege check omits that lever for that actor — a "function incorrect as to spec" gap in the pallet's own security model.

## Impact Explanation
During an active exploit or depeg event on one external asset within a multi-external PSM instance, the intended remediation per the pallet's own workflow documentation is to zero the ceiling weight (or `max_debt`) to throttle minting on just the affected external while leaving redemptions (and other externals) unaffected — this is explicitly described as the "Asset Offboarding Workflow" [7](#0-6) . Because `can_set_asset_ceiling`/`can_set_max_debt` reject the `Emergency` level, the `emergency_admin` is forced to either escalate to `AllDisabled` via the circuit breaker (halting minting **and** redemption for legitimate users on that external, and potentially triggering `AllSwapsStopped` pallet-wide behavior across other externals sharing the aggregate `max_debt`) or wait on the `full_admin` — which per the design's intent could be a slower governance-controlled origin — to act. This degrades the availability/precision of the protocol's own documented emergency-response model and can prolong exposure of the PSM reserve (fund-at-risk window) during an active incident, matching the "function of the protocol or its availability could be impacted" impact class from the original report.

## Likelihood Explanation
This triggers deterministically any time an `emergency_admin` (a legitimately provisioned, non-privileged-in-the-sense-of-Full origin) attempts to use the exact remediation path the pallet's README instructs it to use. No malicious actor, governance abuse, or leaked key is required — it is a pure logic/spec mismatch reachable by the intended emergency responder performing their documented duty, which is why the original C4 finding was accepted as Medium ("function incorrect as to spec" + "critical feature missing from the project's security model") despite requiring a privileged (but intentionally scoped) caller.

## Recommendation
Update `PsmManagerLevel::can_set_max_debt` and `can_set_asset_ceiling` to `matches!(self, PsmManagerLevel::Full | PsmManagerLevel::Emergency)`, matching `can_set_circuit_breaker` and the README's documented privilege table, or alternatively correct the README if `Full`-only was the intended design (and then also fix the `Error::InsufficientPrivilege` docstring's implication that this is a blanket "Full vs Emergency" gate reused inconsistently across extrinsics). Add regression tests asserting `emergency_admin` can successfully call `set_max_debt` and `set_asset_ceiling_weight`.

## Proof of Concept
1. `create_psm` an instance with distinct `full_admin` and `emergency_admin` origins, per `Pallet::create_psm` [8](#0-7) .
2. Approve two externals; simulate an incident on one external requiring the ceiling weight to be zeroed per the documented "Asset Offboarding Workflow" [7](#0-6) .
3. Dispatch `set_asset_ceiling_weight`/`set_max_debt` from the `emergency_admin` origin.
4. Observe the call fails with `Error::InsufficientPrivilege` because `PsmManagerLevel::Emergency` fails `can_set_asset_ceiling`/`can_set_max_debt` [2](#0-1) , contradicting the README's "Full or Emergency" specification for these exact extrinsics.

### Citations

**File:** substrate/frame/psm/README.md (L112-120)
```markdown
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

**File:** substrate/frame/psm/README.md (L133-144)
```markdown
### Asset Offboarding Workflow

For an external `asset_id` on instance `internal_asset`:

1. Set the external's ceiling weight to `0%` (or use `set_asset_status(.., MintingDisabled)`):
   either pauses new minting while still allowing redemptions
2. Redemptions slowly drain `PsmDebt[internal_asset, asset_id]`
3. Once debt reaches zero, call `remove_external_asset(internal_asset, asset_id)`

Lowering a ceiling weight (or `max_debt`) below outstanding debt is allowed: the ceiling is a
mint-time throttle, so the external simply cannot be minted until redemptions bring its debt
back under the new ceiling.
```

**File:** substrate/frame/psm/src/lib.rs (L189-216)
```rust
	/// Privilege level of an origin acting on a PSM instance.
	///
	/// Resolved by matching the incoming origin against the instance's stored
	/// [`PsmAdminInfo::full_admin`] (`Full`) or [`PsmAdminInfo::emergency_admin`]
	/// (`Emergency`), enabling tiered authorization over the instance's parameters.
	#[derive(
		Encode,
		Decode,
		DecodeWithMemTracking,
		MaxEncodedLen,
		TypeInfo,
		Clone,
		Copy,
		PartialEq,
		Eq,
		Debug,
		Default,
	)]
	pub enum PsmManagerLevel {
		/// Full administrative access, held by the instance's `full_admin`.
		/// Can modify all parameters including fees, ceilings, and asset management,
		/// reassign admins, and remove the instance.
		#[default]
		Full,
		/// Emergency access, held by the instance's `emergency_admin`.
		/// Can modify circuit breaker status.
		Emergency,
	}
```

**File:** substrate/frame/psm/src/lib.rs (L224-240)
```rust
		/// Whether this level allows modifying the circuit breaker status.
		/// Both Full and Emergency levels can set circuit breaker.
		pub const fn can_set_circuit_breaker(&self) -> bool {
			matches!(self, PsmManagerLevel::Full | PsmManagerLevel::Emergency)
		}

		/// Whether this level allows modifying the PSM debt ceiling.
		/// Only Full can set the debt ceiling.
		pub const fn can_set_max_debt(&self) -> bool {
			matches!(self, PsmManagerLevel::Full)
		}

		/// Whether this level allows modifying per-asset ceiling weights.
		/// Only Full can set asset ceiling weights.
		pub const fn can_set_asset_ceiling(&self) -> bool {
			matches!(self, PsmManagerLevel::Full)
		}
```

**File:** substrate/frame/psm/src/lib.rs (L634-636)
```rust
		/// Operation requires the instance's `full_admin` (Full level); the caller only
		/// matched the `emergency_admin` (Emergency level).
		InsufficientPrivilege,
```

**File:** substrate/frame/psm/src/lib.rs (L940-950)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::create_psm())]
		pub fn create_psm(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			full_admin: Box<T::PalletsOrigin>,
			emergency_admin: Box<T::PalletsOrigin>,
			fee_destination: T::AccountId,
			max_debt: BalanceOf<T>,
			min_swap_amount: BalanceOf<T>,
		) -> DispatchResult {
```
