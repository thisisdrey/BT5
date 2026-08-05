## Analysis

The external report's core broken invariant: a privileged "set admin" setter accepts an unvalidated new-admin value that can never satisfy future authorization checks, permanently disabling admin-gated functionality with no recovery path.

The direct local analog is `pallet-psm`'s admin-reassignment extrinsics.

## Title
Unvalidated `new_admin` in `Psm::set_full_admin`/`set_emergency_admin` permits permanently unreachable PSM admin, bricking the instance and locking collateral - (File: `substrate/frame/psm/src/lib.rs`)

## Summary
`Pallet::set_full_admin` and `Pallet::set_emergency_admin` let the current `full_admin` overwrite `PsmAdminInfo::full_admin` / `emergency_admin` with an arbitrary caller-supplied `T::PalletsOrigin` value, without any check that the new value can ever again satisfy `ensure_psm_admin`. This mirrors `LibOwnable._setAdmin` accepting `address(0)`: the setter has no validation on the new privileged identity, so the privileged role can be assigned to a value that never matches any real future origin, permanently disabling every admin-gated call on that PSM instance.

## Finding Description
`ensure_psm_admin` authorizes callers purely by equality against the stored `PsmAdminInfo::full_admin` / `emergency_admin` values: [1](#0-0) 

`set_full_admin` writes `new_admin` into storage after only checking that the *caller* currently holds `Full` privilege — it performs no check that `new_admin` is a legitimate, reachable origin: [2](#0-1) 

`T::PalletsOrigin` is defined generically as any `Parameter + From<frame_system::RawOrigin<AccountId>> + CallerTrait<AccountId>`: [3](#0-2) 

In a runtime this resolves to the aggregated `OriginCaller` enum, which contains variants for every pallet's origin type, including origins that can never be produced by `ensure_origin` in practice (e.g. a collective origin for a body with no members, a pallet origin not wired to any call path in the runtime, or simply a `RawOrigin::Signed` account that nobody controls and that was never meant to be the admin). Because `set_full_admin`/`set_emergency_admin` accept a raw, unchecked `Box<T::PalletsOrigin>`, the current `full_admin` (or an attacker who otherwise gains one signed transaction as `full_admin`, e.g. via a compromised/expired key rotation flow, front-running a scheduled admin handoff, or a copy/paste error citing the wrong pallet-origin variant) can set both `full_admin` and `emergency_admin` to values that will never again equal any real caller's `into_caller()` result.

Since `can_manage_admins()` is `Full`-only, once `full_admin` is unreachable there is no path to ever reassign either admin again — `set_full_admin`, `set_emergency_admin`, `remove_psm`, `add_external_asset`/`remove_external_asset`, `set_max_debt`, `set_asset_ceiling_weight`, and `set_asset_status` (Emergency level too, once `emergency_admin` is likewise orphaned or was never separately controlled) all permanently return `BadOrigin`/`InsufficientPrivilege`. There is no `ForceOrigin`/root override anywhere in this pallet (`ensure_psm_admin` is the only authorization path), and no bound on `Config::PalletsOrigin` preventing degenerate/uninhabited values, unlike the `RiverAddress`-style explicit zero-address checks the original report contrasts against.

## Impact Explanation
Once a PSM's `full_admin` is bricked, the circuit breaker (`CircuitBreakerLevel`) and asset ceiling weights are frozen at whatever values were last set. If minting/redemption was in any restricted state (`MintingDisabled` or `AllDisabled`) at the time of the bad reassignment — or is later forced into that state via `emergency_admin` before it too is orphaned — it can never be reverted. Outstanding `PsmDebt` and the PSM's held external-asset collateral (`Psm::psm_account`) become permanently locked: redemption may be blocked, and `remove_external_asset`/`remove_psm` (which require zero debt and `can_manage_admins`/`can_remove_psm`, both Full-only) can never be called to unwind the instance and release its provider references and reserved deposit. This matches the "permanent user-fund or bridge-state lock" impact category.

## Likelihood Explanation
No special privilege beyond a single valid `full_admin`-authenticated call is required — this is the exact privilege level the extrinsic is designed to accept, but the extrinsic itself performs zero validation on the target value, unlike comparable pallets (e.g. `nfts`/`uniques` `set_team`, `assets::create`) which at least route new roles through `T::Lookup` and require them to be real account IDs. A single malformed/mistaken `set_full_admin(internal_asset, Box::new(unreachable_origin))` call — whether from operator error, a scripting bug picking the wrong `OriginCaller` variant, or deliberate sabotage by a soon-to-be-revoked admin — is sufficient and irreversible.

## Recommendation
Add validation in `set_full_admin`/`set_emergency_admin` (and `create_psm`, which sets both admins unchecked at `substrate/frame/psm/src/lib.rs:966-986`) rejecting degenerate/known-unreachable `T::PalletsOrigin` values, or require `Config` to expose a way to prove the supplied origin is currently satisfiable (e.g. constrain to `RawOrigin::Signed`/`Root` only, or require re-deriving `into_caller()` from an actual `ensure_origin` call on a companion parameter rather than accepting an opaque `PalletsOrigin` blob). At minimum, provide a `ForceOrigin`/root-level recovery extrinsic to reset `PsmAdminInfo` when the pallet has no other override path, mirroring how other pallets keep a `ForceOrigin` escape hatch for team/owner recovery.

## Proof of Concept
1. `full_admin` (attacker-controlled account `A`) calls `Psm::set_full_admin(signed(A), internal_asset, Box::new(OriginCaller::SomeNeverDispatchedPalletOrigin(..)))`.
2. `ensure_psm_admin` succeeds (caller `A` still matches stored `full_admin` at call time) and the write completes via `PsmAdmin::try_mutate` at `substrate/frame/psm/src/lib.rs:1439-1446`.
3. Any subsequent call to `set_full_admin`, `set_emergency_admin`, `remove_psm`, `add_external_asset`, `remove_external_asset`, `set_max_debt`, or `set_asset_ceiling_weight` for `internal_asset` now fails `ensure_psm_admin`'s equality check permanently (`substrate/frame/psm/src/lib.rs:1666-1671`), since no caller can ever produce the orphaned `OriginCaller` value.
4. If `CircuitBreakerLevel::AllDisabled` or a restrictive ceiling weight was set beforehand, the PSM's collateral and internal-asset debt become permanently frozen with no admin path to remediate.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L358-363)
```rust
		/// The caller origin, overarching type of all pallets' origins. Stored as a PSM's
		/// `full_admin` / `emergency_admin` and matched against incoming origins.
		type PalletsOrigin: Parameter
			+ From<frame_system::RawOrigin<Self::AccountId>>
			+ CallerTrait<Self::AccountId>
			+ MaxEncodedLen;
```

**File:** substrate/frame/psm/src/lib.rs (L1430-1453)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(T::WeightInfo::set_full_admin())]
		pub fn set_full_admin(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			new_admin: Box<T::PalletsOrigin>,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_manage_admins())?;
			let new_admin = *new_admin;
			let old_admin = PsmAdmin::<T>::try_mutate(
				&internal_asset,
				|maybe| -> Result<T::PalletsOrigin, DispatchError> {
					let admin = maybe.as_mut().ok_or(Error::<T>::PsmNotFound)?;
					let old = core::mem::replace(&mut admin.full_admin, new_admin.clone());
					Ok(old)
				},
			)?;
			Self::deposit_event(Event::FullAdminChanged {
				internal_asset,
				old_admin: Box::new(old_admin),
				new_admin: Box::new(new_admin),
			});
			Ok(())
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1659-1675)
```rust
		pub(crate) fn ensure_psm_admin(
			origin: OriginFor<T>,
			internal_asset: &T::AssetId,
			required: impl Fn(PsmManagerLevel) -> bool,
		) -> DispatchResult {
			let admin = PsmAdmin::<T>::get(internal_asset).ok_or(Error::<T>::PsmNotFound)?;
			let caller = <T as Config>::RuntimeOrigin::from(origin).into_caller();
			let level = if caller == admin.full_admin {
				PsmManagerLevel::Full
			} else if caller == admin.emergency_admin {
				PsmManagerLevel::Emergency
			} else {
				return Err(DispatchError::BadOrigin);
			};
			ensure!(required(level), Error::<T>::InsufficientPrivilege);
			Ok(())
		}
```
