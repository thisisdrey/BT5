### Title
Irreversible fee-asset burn before unguarded `Transact` dispatch in `snowbridge-pallet-system-frontend` can permanently destroy user funds on call mismatch - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet_system_frontend::add_tip` and `register_token` burn (permanently destroy, not escrow) the caller's fee/tip asset via `swap_fee_asset_and_burn` → `burn_for_teleport` *before* the pallet dispatches a remote `Transact` XCM to BridgeHub built from a hand-maintained, hardcoded call/pallet index enum (`BridgeHubRuntime`/`EthereumSystemCall`). If the encoded `(pallet_index, call_index)` does not match the actual dispatchable slot on BridgeHub (e.g. after a pallet reorder/runtime upgrade on the BridgeHub side, since these indices are duplicated by hand in this crate rather than derived from the real runtime), the `Transact` fails to decode/dispatch there, and because the outbound XCM program has no `SetAppendix`/refund logic, the already-burned funds are unrecoverable. This mirrors the `L1ERC1155Bridge.finalizeWithdrawalBatch()` bug class: a wrong selector/index causes the remote call to fail, and the already-committed value transfer cannot be reversed, so the user loses funds.

### Finding Description
`EthereumSystemCall`/`BridgeHubRuntime` are minimized, manually duplicated representations of the real `snowbridge-pallet-system-v2` call enum on BridgeHub, with codec indices hardcoded as constants: [1](#0-0) 

`add_tip` first burns the user's swapped-to-ether tip via `swap_fee_asset_and_burn`, which internally calls `burn_for_teleport` — an irreversible `withdraw_asset` from the origin with no compensating mint anywhere in this pallet: [2](#0-1) [3](#0-2) 

Only *after* the burn does the pallet build and send the `Transact` call to BridgeHub: [4](#0-3) [5](#0-4) 

Unlike the inbound-queue converter for `register_token`-style flows elsewhere in Snowbridge, which explicitly wraps the `Transact` in a `SetAppendix([RefundSurplus, DepositAsset{...}])` to protect against `Transact` failing or leaving fees stranded: [6](#0-5) 

`build_remote_xcm` in the system-frontend pallet has **no appendix, no error-check instruction, and no refund path** — it is a bare `DescendOrigin` + `UnpaidExecution` + `Transact`. `send_xcm`/`send_transact_call` only report a `SendFailure`/`Unreachable` error if the *local* XCM router itself fails to queue the message; they cannot detect or react to the *remote* dispatch failing to decode or execute on BridgeHub: [7](#0-6) 

Because `EthereumSystemCall`/`BridgeHubRuntime` indices are hand-copied constants rather than generated/verified against the actual BridgeHub runtime `Call` enum, any drift between the two crates (pallet reordering, `call_index` renumbering, or forgetting to bump this frontend crate on a BridgeHub runtime upgrade) produces exactly the "wrong selector" failure mode from the reported bug: the remote `Transact` fails to decode into a valid extrinsic, `EthereumSystem::register_token`/`add_tip` never executes, and the caller's burned ether is gone with nothing to show for it.

### Impact Explanation
This directly matches the "Theft or unbacked mint or unlock" / "permanent user-fund ... lock" impact class: user funds (swapped-and-burned ether meant to fund an Ethereum-side registration or relayer tip) are destroyed unconditionally on the local chain, while the remote effect (asset registration / tip credit) that was supposed to justify the burn can silently fail to happen if the hardcoded call indices ever diverge from the live BridgeHub runtime. Unlike other Snowbridge outbound flows in this codebase that defensively wrap `Transact` in `SetAppendix`/`RefundSurplus`, this path has none, so there is no fallback state to recover the burned value.

### Likelihood Explanation
The vulnerability is not triggered by an attacker directly, but is a structural design gap: it activates whenever the BridgeHub runtime's `EthereumSystem` pallet index or the `register_token`/`add_tip` call indices change relative to the hardcoded values `#[codec(index = 90)]` and `#[codec(index = 2/3)]` in this crate, which is plausible across ordinary runtime upgrades since the mapping is maintained by hand across two separate crates/repos rather than being generated or asserted at compile/CI time. Every unprivileged caller of `add_tip`/`register_token` is affected the moment such drift exists, with no privileged actor, malicious relayer, or governance abuse required — it is a pure implementation-bug in the public dispatch/message-routing path.

### Recommendation
1. Add a compile-time or CI-enforced check that `EthereumSystemCall`/`BridgeHubRuntime` indices in `snowbridge-pallet-system-frontend` match the actual `snowbridge-pallet-system-v2` `Call` enum indices/pallet index used in each BridgeHub runtime (similar to the `ensure_macro_compatibility_for_generate_receive_message_proof_call_builder` pattern already used for `pallet-bridge-messages`, see `bridges/relays/lib-substrate-relay/src/messages/mod.rs`).
2. Wrap the outbound `Transact` in `build_remote_xcm` with a `SetAppendix`/`RefundSurplus`-style safety net, or restructure the flow so the burn happens only after confirmation that the remote call executed successfully (e.g., swap the order: reserve/mark-pending locally, burn only upon a delivery/execution receipt), consistent with the "message queues ... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" requirement.
3. Add integration/end-to-end tests that intentionally desynchronize the local hardcoded indices from a mock BridgeHub runtime to confirm burned funds are not lost silently.

### Proof of Concept
1. Deploy a BridgeHub runtime where the `EthereumSystem` (`snowbridge-pallet-system-v2`) pallet's on-chain index or its `register_token`/`add_tip` `call_index` differs from the constants hardcoded in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` (`#[codec(index = 90)]`, `#[codec(index = 2)]`, `#[codec(index = 3)]`) — this occurs naturally whenever the frontend crate is not synchronously updated with a BridgeHub pallet reorder.
2. A user calls `add_tip(message_id, asset)` on the parachain running `system-frontend`.
3. `swap_fee_asset_and_burn` swaps `asset` for ether and calls `burn_for_teleport`, irreversibly withdrawing the ether from the user's account (`bridges/snowbridge/primitives/core/src/lib.rs:192-200`).
4. `send_transact_call` builds and sends the XCM `Transact` containing the stale-index-encoded `EthereumSystemCall::AddTip` to BridgeHub (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:406-423`).
5. On BridgeHub, the XCM executor's `Transact` fails to decode the call into a real dispatchable (wrong pallet/call index) and the instruction errors out with no compensating logic (no appendix), leaving the user's burned ether permanently lost and no tip ever registered.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L277-285)
```rust
		fn send_xcm(origin: Location, dest: Location, xcm: Xcm<()>) -> Result<XcmHash, SendError> {
			let is_waived =
				<T::XcmExecutor as FeeManager>::is_waived(Some(&origin), FeeReason::ChargeFees);
			let (ticket, price) = validate_send::<T::XcmSender>(dest, xcm.clone())?;
			if !is_waived {
				T::XcmExecutor::charge_fees(origin, price).map_err(|_| SendError::Fees)?;
			}
			T::XcmSender::deliver(ticket)
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
