## Analysis

Confirmed: `close_bridge` in `pallet-xcm-bridge-hub` uses the **stored** `bridge.bridge_owner_account` (persisted at `open_bridge` time) to refund the `BridgeDeposit`, rather than recomputing it from `bridge_origin_relative_location` at close time. [1](#0-0) 
The value is derived once via `T::BridgeOriginAccountIdConverter::convert_location(...)` inside `do_open_bridge` and cached in the `Bridges` storage map. [2](#0-1) 
The pallet's own `try-state`/migration checks acknowledge that this cached value can drift from what the current `BridgeOriginAccountIdConverter` would compute, explicitly flagging: `"bridge.bridge_owner_account" is different than calculated from "bridge.bridge_origin_relative_location", needs migration!` [3](#0-2) 

### Title
Stale cached `bridge_owner_account` in `pallet-xcm-bridge-hub` causes deposit misdirection after `LocationToAccountId`/converter upgrade - (File: `bridges/modules/xcm-bridge-hub/src/lib.rs`)

### Summary
`pallet-xcm-bridge-hub::do_open_bridge` computes `bridge_owner_account` once via `T::BridgeOriginAccountIdConverter::convert_location(&bridge_origin_relative_location)` and persists it in the `Bridges` storage entry for the lifetime of the bridge. `close_bridge` later releases the held `BridgeDeposit` to this **cached** account rather than recomputing it from the stored `bridge_origin_relative_location` at closure time. This is structurally identical to the `Reserve.sol`/`DAO.sol` bug: one authoritative identity (the sovereign-account-derivation logic driven by `LocationToAccountId`/XCM version config) can change via a runtime upgrade, while a dependent storage item (`Bridges::bridge_owner_account`) keeps referencing the old, now-stale, value. The pallet's own try-runtime invariant explicitly anticipates this drift and calls for "needs migration," confirming the maintainers know the value can silently desynchronize.

### Finding Description
- `Bridge.bridge_owner_account` is set exactly once at `open_bridge` time from the sovereign account derived for the caller's XCM origin location. [2](#0-1) 
- Any runtime upgrade that changes how locations map to accounts (e.g. a new/patched `LocationToAccountId`/`BridgeOriginAccountIdConverter` implementation, a new XCM version's canonicalization rules, or a fix to the sovereign-account-derivation algorithm — all ordinary, non-privileged-abuse protocol evolution, analogous to the "DAO upgrade" in the original report) changes what `convert_location` would return for the *same* `bridge_origin_relative_location`.
- `close_bridge` never recomputes the owner account; it unconditionally pays the deposit refund to the old, stored `bridge_owner_account`. [4](#0-3) 
- The mismatch is only ever detected passively, by an optional `try-state`/migration check that emits a `TryRuntimeError`, it is not enforced on the hot path of `close_bridge`. [3](#0-2) 
- Just like `Reserve.sol`'s `onlyGrantor` check comparing against a stale `DAO` address instead of the current one, this pallet compares/uses a stale derived account instead of re-deriving it, so the "source of truth" (current converter logic) and the "cached copy" (`bridge_owner_account` in storage) diverge after the underlying identity/authority mapping changes.

### Impact Explanation
If `bridge_origin_relative_location` no longer maps to the same account under the updated converter (this can legitimately happen for accounts whose sovereign-account derivation changes format, e.g. 32-byte vs 20-byte account hashing changes, `Parents`/`Junctions` encoding changes, or a bug-fix to the converter itself), the `BridgeDeposit` held under `HoldReason::BridgeDeposit` for the *current* rightful owner is released to an account that is no longer the legitimate sovereign account for that origin. This is a fund-misdirection issue: the deposit is not conserved to the rightful beneficiary as required ("Balances ... must conserve value and settle exactly once to the rightful beneficiary and amount"). Because `close_bridge` also permanently prunes the bridge/lane state (`Bridges::remove`, lane `purge()`), the mis-paid deposit cannot be recovered by the actual current owner — a permanent, one-time loss of the `BridgeDeposit` for the affected sibling chain/relay origin.

### Likelihood Explanation
This does not require a malicious peer, validator, relayer, or governance abuse — it is triggered purely by the passage of a legitimate, one-time runtime/config upgrade to the location→account conversion logic (a maintenance change parity between chains regularly performs, comparable to the "DAO upgrade" trigger in the source report) followed by an ordinary, permissionless `close_bridge` call from the bridge's actual current owner. The pallet authors' own migration-check message shows this is a recognized, real drift scenario rather than a purely theoretical one; however, exploitation depends on such a converter change actually occurring on a live Bridge Hub runtime, which is an infrequent but foreseeable event (XCM version bumps, sovereign-account algorithm fixes).

### Recommendation
On `close_bridge` (and ideally on every state-mutating access of `Bridge.bridge_owner_account`), recompute the sovereign account from `T::BridgeOriginAccountIdConverter::convert_location(&bridge.bridge_origin_relative_location)` and either (a) use the freshly computed value for release, or (b) if it differs from the stored value, refuse the release and require an explicit governance/migration step to reconcile balances before funds move, mirroring the model suggested for `Reserve.sol`: refresh dependent references (`setIncentiveAddresses`-style resync) whenever the authoritative identity changes, instead of trusting a value cached at creation time.

### Proof of Concept
1. On a Bridge Hub runtime, a sibling parachain opens a bridge via `open_bridge`; `bridge_owner_account = convert_location(origin_relative_location)` is computed under `LocationToAccountId = V1` and stored in `Bridges`, with `BridgeDeposit` held on that account.
2. A subsequent runtime upgrade replaces `BridgeOriginAccountIdConverter`/`LocationToAccountId` with a `V2` implementation (e.g., updated XCM version handling), which computes a *different* account for the same `bridge_origin_relative_location`. No migration is run to update existing `Bridges` entries (the pallet only exposes a passive try-state check for this, not an automatic fix).
3. The legitimate origin calls `close_bridge` for its bridge, permissionlessly (any bridge owner can do this for its own bridge). `close_bridge` releases `bridge.deposit` to the storage's stale `bridge_owner_account` (the V1-derived account) rather than the V2-derived account that would be the actual current sovereign account for that origin under the new converter.
4. The bridge and lane are permanently purged (`Bridges::remove`, lane `purge()`), so this refund path can never be retried or corrected — the deposit has settled to the wrong account, one-time and unrecoverable, exactly mirroring `grantFunds` sending funds based on a stale authority-linked address.

### Citations

**File:** bridges/modules/xcm-bridge-hub/src/lib.rs (L408-432)
```rust
			// else we have pruned all messages, so lanes and the bridge itself may gone
			inbound_lane.purge();
			outbound_lane.purge();
			Bridges::<T, I>::remove(locations.bridge_id());
			LaneToBridge::<T, I>::remove(bridge.lane_id);

			// return deposit
			let released_deposit = T::Currency::release(
				&HoldReason::BridgeDeposit.into(),
				&bridge.bridge_owner_account,
				bridge.deposit,
				Precision::BestEffort,
			)
			.inspect_err(|e| {
				// we can't do anything here - looks like funds have been (partially) unreserved
				// before by someone else. Let's not fail, though - it'll be worse for the caller
				tracing::error!(
					target: LOG_TARGET,
					error=?e,
					bridge_id=?locations.bridge_id(),
					"Failed to unreserve during the bridge closure"
				);
			})
			.ok()
			.unwrap_or(BalanceOf::<ThisChainOf<T, I>>::zero());
```

**File:** bridges/modules/xcm-bridge-hub/src/lib.rs (L463-513)
```rust
		) -> Result<(), DispatchError> {
			// reserve balance on the origin's sovereign account (if needed)
			let bridge_owner_account = T::BridgeOriginAccountIdConverter::convert_location(
				locations.bridge_origin_relative_location(),
			)
			.ok_or(Error::<T, I>::InvalidBridgeOriginAccount)?;
			let deposit = if T::AllowWithoutBridgeDeposit::contains(
				locations.bridge_origin_relative_location(),
			) {
				BalanceOf::<ThisChainOf<T, I>>::zero()
			} else {
				let deposit = T::BridgeDeposit::get();
				T::Currency::hold(
					&HoldReason::BridgeDeposit.into(),
					&bridge_owner_account,
					deposit,
				)
				.map_err(|e| {
					tracing::error!(
						target: LOG_TARGET,
						error=?e,
						?deposit,
						?bridge_owner_account,
						bridge_origin_relative_location=?locations.bridge_origin_relative_location(),
						"Failed to hold bridge deposit"
					);
					Error::<T, I>::FailedToReserveBridgeDeposit
				})?;
				deposit
			};

			// save bridge metadata
			Bridges::<T, I>::try_mutate(locations.bridge_id(), |bridge| match bridge {
				Some(_) => Err(Error::<T, I>::BridgeAlreadyExists),
				None => {
					*bridge = Some(BridgeOf::<T, I> {
						bridge_origin_relative_location: Box::new(
							locations.bridge_origin_relative_location().clone().into(),
						),
						bridge_origin_universal_location: Box::new(
							locations.bridge_origin_universal_location().clone().into(),
						),
						bridge_destination_universal_location: Box::new(
							locations.bridge_destination_universal_location().clone().into(),
						),
						state: BridgeState::Opened,
						bridge_owner_account,
						deposit,
						lane_id,
					});
					Ok(())
```

**File:** bridges/modules/xcm-bridge-hub/src/lib.rs (L1552-1573)
```rust
			// error bridge owner account cannot be calculated
			test_bridge_state(
				bridge_id,
				Bridge {
					bridge_origin_relative_location: Box::new(VersionedLocation::from(
						bridge_origin_relative_location.clone(),
					)),
					bridge_origin_universal_location: Box::new(VersionedInteriorLocation::from(
						bridge_origin_universal_location.clone(),
					)),
					bridge_destination_universal_location: Box::new(VersionedInteriorLocation::from(
						bridge_destination_universal_location.clone(),
					)),
					state: BridgeState::Opened,
					bridge_owner_account: bridge_owner_account_mismatch.clone(),
					deposit: Zero::zero(),
					lane_id,
				},
				(lane_id, bridge_id),
				(lane_id, lane_id),
				Some(TryRuntimeError::Other("`bridge.bridge_owner_account` is different than calculated from `bridge.bridge_origin_relative_location`, needs migration!")),
			);
```
