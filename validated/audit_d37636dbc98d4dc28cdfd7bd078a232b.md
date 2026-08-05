Audit Report

## Title
Irreversible fee-asset burn before unguarded `Transact` dispatch in `snowbridge-pallet-system-frontend` can permanently destroy user funds on call mismatch - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`add_tip` and `register_token` in `system-frontend` irreversibly burn the caller's fee/tip asset via `swap_fee_asset_and_burn` → `burn_for_teleport` before sending a `Transact` XCM to BridgeHub, encoded using hand-maintained, hardcoded call/pallet indices (`BridgeHubRuntime`/`EthereumSystemCall` with `#[codec(index = 90)]`, `2`, `3`). If these indices ever diverge from the real `snowbridge-pallet-system-v2` `Call` enum on BridgeHub (pallet reorder, call re-indexing, or an unsynchronized crate upgrade), the remote `Transact` fails to decode/dispatch and, since `build_remote_xcm` contains no `SetAppendix`/refund logic, the already-burned funds are permanently lost with no compensating credit.

## Finding Description
`EthereumSystemCall`/`BridgeHubRuntime` are locally-defined duplicates of the real BridgeHub-side call enum, with codec indices hardcoded as constants rather than derived from or verified against `snowbridge-pallet-system-v2`: [1](#0-0) 

`add_tip` calls `swap_fee_asset_and_burn`, which performs an irreversible `withdraw_asset` via `burn_for_teleport` with no compensating mint anywhere in this pallet, before building and dispatching the `Transact` call: [2](#0-1) [3](#0-2) 

`build_remote_xcm` constructs a bare `DescendOrigin` + `UnpaidExecution` + `Transact` sequence with no `SetAppendix`/`RefundSurplus` safety net, unlike the inbound-queue converter elsewhere in Snowbridge which explicitly wraps `Transact` with a refund appendix: [4](#0-3) [5](#0-4) 

`send_xcm`/`send_transact_call` only surface `SendFailure`/`Unreachable` errors from the *local* router failing to queue the message — they cannot detect remote dispatch/decode failure on BridgeHub: [6](#0-5) 

However, I was unable to verify within the available tooling that an actual index mismatch currently exists between the hardcoded `EthereumSystemCall`/`BridgeHubRuntime` constants in `system-frontend` and the live `snowbridge-pallet-system-v2` pallet as deployed in `bridge-hub-westend`'s `construct_runtime!` (pallet index assignment and `#[pallet::call_index]` values on the real `Call` enum). The claim itself concedes this is a structural/maintenance risk that "activates whenever" a future divergence occurs — it does not demonstrate a divergence that exists in this repository today.

## Impact Explanation
As described, this is a real design gap: the burn-then-Transact ordering with no appendix/refund path in `system-frontend` is structurally different from other Snowbridge outbound flows (e.g., `inbound-queue`'s `SetAppendix([RefundSurplus, ...])` pattern), and if the hardcoded indices ever go stale, users' burned ether would be unrecoverable, matching the "permanent user-fund lock"/"theft or unbacked... burn" impact class.

## Likelihood Explanation
The claim itself states the precondition for exploitation is that the two independently-maintained crates' call/pallet indices diverge — a future maintenance/deployment-synchronization scenario, not a currently exploitable state in this repository's code as reviewed. No unprivileged-attacker-reachable path was demonstrated that triggers this today; it requires a hypothetical future runtime-upgrade misconfiguration on the BridgeHub side, which falls outside "reachable exploit path from attacker input to bad state" using the current, in-repo runtime wiring. I could not confirm or refute the current index-correctness within the remaining tool budget, so I cannot assert this is a live divergence in this repository as opposed to a general defense-in-depth improvement suggestion (missing refund appendix) applied to a currently-consistent index mapping.

## Recommendation
1. Add a CI/compile-time equality check between `EthereumSystemCall`/`BridgeHubRuntime` indices in `system-frontend` and the actual `snowbridge-pallet-system-v2` `Call` enum/pallet index used in each BridgeHub runtime.
2. Wrap the outbound `Transact` in `build_remote_xcm` with a `SetAppendix`/`RefundSurplus`-style safety net, or reorder the flow so the burn occurs only after confirmation of remote execution success.
3. Add integration tests that intentionally desynchronize local indices from a mock BridgeHub runtime to confirm burned funds are not silently lost.

## Proof of Concept
1. Deploy/point a BridgeHub runtime where `EthereumSystem`'s pallet index or `register_token`/`add_tip` `call_index` differs from `system-frontend`'s hardcoded constants (`90`, `2`, `3`).
2. A user calls `add_tip(message_id, asset)` on the parachain running `system-frontend`.
3. `swap_fee_asset_and_burn`/`burn_for_teleport` irreversibly withdraws the ether from the user.
4. `send_transact_call` dispatches the stale-index-encoded `Transact` to BridgeHub.
5. The XCM executor on BridgeHub fails to decode the call into a valid dispatchable; with no appendix/refund logic, the burned ether is permanently lost and no tip is registered.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L48-68)
```rust
/// Call indices within BridgeHub runtime for dispatchables within `snowbridge-pallet-system-v2`
#[allow(clippy::large_enum_variant)]
#[derive(Encode, Decode, Debug, PartialEq, Clone, TypeInfo)]
pub enum BridgeHubRuntime<T: frame_system::Config> {
	#[codec(index = 90)]
	EthereumSystem(EthereumSystemCall<T>),
}

/// Call indices for dispatchables within `snowbridge-pallet-system-v2`
#[derive(Encode, Decode, Debug, PartialEq, Clone, TypeInfo)]
pub enum EthereumSystemCall<T: frame_system::Config> {
	#[codec(index = 2)]
	RegisterToken {
		sender: Box<VersionedLocation>,
		asset_id: Box<VersionedLocation>,
		metadata: AssetMetadata,
		amount: u128,
	},
	#[codec(index = 3)]
	AddTip { sender: AccountIdOf<T>, message_id: MessageId, amount: u128 },
}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L353-363)
```rust
		fn build_remote_xcm(call: &impl Encode) -> Xcm<()> {
			Xcm(vec![
				DescendOrigin(T::PalletLocation::get()),
				UnpaidExecution { weight_limit: Unlimited, check_origin: None },
				Transact {
					origin_kind: OriginKind::Xcm,
					call: call.encode().into(),
					fallback_max_weight: None,
				},
			])
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L406-423)
```rust
		fn send_transact_call(
			origin_location: Location,
			call: BridgeHubRuntime<T>,
		) -> DispatchResult {
			let dest = T::BridgeHubLocation::get();
			let remote_xcm = Self::build_remote_xcm(&call);
			let message_id = Self::send_xcm(origin_location, dest.clone(), remote_xcm.clone())
				.map_err(|error| Error::<T>::from(error))?;

			Self::deposit_event(Event::<T>::MessageSent {
				origin: T::PalletLocation::get().into(),
				destination: dest,
				message: remote_xcm,
				message_id,
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/primitives/core/src/lib.rs (L192-200)
```rust
pub fn burn_for_teleport<AssetTransactor>(origin: &Location, fee: &Asset) -> XcmResult
where
	AssetTransactor: TransactAsset,
{
	let dummy_context = XcmContext { origin: None, message_id: Default::default(), topic: None };
	AssetTransactor::can_check_out(origin, fee, &dummy_context)?;
	AssetTransactor::check_out(origin, fee, &dummy_context);
	AssetTransactor::withdraw_asset(fee, origin, None)?;
	Ok(())
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L264-270)
```rust
			// This `SetAppendix` ensures that `xcm_fee` not spent by `Transact` will be
			// deposited to snowbridge sovereign, instead of being trapped, regardless of
			// `Transact` success or not.
			SetAppendix(Xcm(vec![
				RefundSurplus,
				DepositAsset { assets: AllCounted(1).into(), beneficiary: bridge_location },
			])),
```
