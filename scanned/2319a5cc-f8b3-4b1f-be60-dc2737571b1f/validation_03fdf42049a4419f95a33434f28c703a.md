## Analysis

The external report's root cause is a **hardcoded, unverified call encoding** (function selector) that must exactly match a different contract's actual dispatchable signature. If that binding silently drifts, calls fail or dispatch the wrong logic, and users lose access to funds/rewards.

The closest local analog in `paritytech/polkadot-sdk` (Kohvert fork) is the **hardcoded cross-runtime pallet/call index** used by Snowbridge's inbound-queue message converter to build a `Transact` call that registers a foreign asset on Asset Hub from a message originating on Ethereum/BridgeHub.

### Title
Hardcoded, unverified `CreateAssetCallIndex` for cross-runtime `Transact` can silently break Snowbridge foreign-asset registration and permanently trap bridged funds - (File: `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs`)

### Summary
BridgeHub encodes an XCM `Transact` call meant to invoke `create` on Asset Hub's `ForeignAssets` pallet using a raw, hardcoded `[pallet_index, call_index]` byte pair. Unlike the local inbound-queue pallet indices, this cross-runtime call index has no corresponding assertion tying it to the actual pallet/call index on the target runtime, so it can silently desynchronize — exactly the same failure class as the reported `withdraw(uint256,address,bool)` vs `withdraw(uint256,bool)` selector mismatch.

### Finding Description
`CreateAssetCallIndex` is declared as a raw constant: [1](#0-0) 

It is fed into `MessageToXcm` as the `CreateAssetCall` generic parameter, which uses it to build a raw tuple-encoded `Transact` call targeting the `ForeignAssets::create` (or equivalent) dispatchable on Asset Hub: [2](#0-1) 

This is structurally identical to the audit finding: a hardcoded byte-level call signature (`create_call_index`) is trusted to match the real dispatchable's `(pallet_index, call_index)` on a separate codebase/runtime, with no compile-time or runtime enforcement that the two stay in sync.

Crucially, the repository *does* protect the equivalent local coupling — the raw pallet index used for `INBOUND_QUEUE_PALLET_INDEX_V1`/`V2` is checked against `PalletInfoAccess::index()`: [3](#0-2) 

No equivalent test exists asserting that `CreateAssetCallIndex::get()` (`[53, 0]`) matches `<ForeignAssets as PalletInfoAccess>::index()` and the actual `#[pallet::call_index]` of the `create`/`force_create` dispatchable as declared in `pallet_assets`. Because `ForeignAssets`'s pallet index and `pallet_assets`'s call ordering are defined independently in `asset-hub-westend`'s `construct_runtime!`/`#[pallet::call]` block, any reordering, insertion of a new call before `create`, or renumbering of the `ForeignAssets` instance in Asset Hub's runtime upgrade will silently desynchronize this hardcoded byte pair — with nothing in BridgeHub's build or tests to catch it, since BridgeHub and Asset Hub are compiled and upgraded as separate runtimes.

### Impact Explanation
If the hardcoded `(pallet_index, call_index)` no longer matches `ForeignAssets::create`, the SCALE-decoded `Transact` call on Asset Hub will either:
- fail to decode (dispatch error), or
- decode into a *different* dispatchable with a compatible argument shape, causing incorrect state changes under the bridge's XCM-derived origin.

Because the surrounding XCM program already performs `ReceiveTeleportedAsset`, `BuyExecution`, and `DepositAsset` of the deposit into the bridge sovereign **before** the `Transact` executes, a failed/misrouted `create` call means the foreign asset is never properly registered while the deposit/fees for that registration have already moved. Any assets referencing that unregistered `asset_id` (per `convert_send_token`) can no longer be reserve-withdrawn/deposited correctly, resulting in funds becoming permanently stuck in the bridge sovereign or trapped assets on Asset Hub — matching the "permanent user-fund or bridge-state lock" impact class.

### Likelihood Explanation
This requires no malicious relayer, validator, or governance action — it is a pure implementation/maintenance hazard: any future runtime upgrade to `pallet_assets`'s call ordering or the `ForeignAssets` instance's pallet index on Asset Hub, made without also updating this hardcoded constant in the separate BridgeHub crate, silently triggers the issue. The repository's own practice of runtime-testing the pallet index for the inbound queue (but not for this cross-runtime target call) shows this exact class of drift is a known risk that was mitigated for one binding but not the other.

### Recommendation
- Add a runtime/integration test (analogous to `bridge_hub_inbound_queue_pallet_index_is_correct`) in the emulated cross-chain test suite that asserts `CreateAssetCallIndex::get()` matches `<asset_hub_westend_runtime::ForeignAssets as PalletInfoAccess>::index()` and the actual `call_index` attribute of the `create` dispatchable in `pallet_assets`.
- Prefer deriving the call index programmatically (e.g., via a shared minimal call enum with `#[codec(index = ...)]`, as already done for `RelayRuntimePallets`/`BrokerRuntimePallets` in the coretime modules) rather than an opaque `[u8; 2]` constant.
- Add a CI check that fails when Asset Hub's `pallet_assets` call ordering changes without a corresponding update to consumers of hardcoded call indices in BridgeHub.

### Proof of Concept
1. On `asset-hub-westend`, perform a runtime upgrade that inserts a new dispatchable into `pallet_assets`'s `#[pallet::call]` impl before `create`/`force_create` without an explicit `#[pallet::call_index]`, shifting the implicit index of `create` away from `0`.
2. `CreateAssetCallIndex` in `bridge-hub-westend` remains `[53, 0]` (unchanged, since it lives in a separate crate/runtime with independent release cadence).
3. An Ethereum `RegisterToken` message arrives at BridgeHub's inbound queue; `MessageToXcm::convert_register_token` builds a `Transact` using the stale `create_call_index`, per `bridges/snowbridge/primitives/inbound-queue/src/v1.rs` lines 254-287.
4. The XCM executes `ReceiveTeleportedAsset`/`DepositAsset` of the registration deposit into the bridge sovereign on Asset Hub, then dispatches the now-mismatched `Transact` call, which either errors out or invokes the wrong `pallet_assets` dispatchable.
5. The intended foreign asset is never created; the deposit is already spent; any token transfer referencing that `asset_id` fails permanently, trapping bridged value — the same "call reaches the chain but with the wrong function bound to it" failure as the reported `WITHDRAWCLAIM` selector bug.

### Citations

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L88-90)
```rust
parameter_types! {
	pub const CreateAssetCallIndex: [u8;2] = [53, 0];
	pub const SetReservesCallIndex: [u8;2] = [53, 33];
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L457-472)
```rust
	#[test]
	fn bridge_hub_inbound_queue_pallet_index_is_correct() {
		assert_eq!(
			INBOUND_QUEUE_PALLET_INDEX_V1,
			<EthereumInboundQueue as frame_support::traits::PalletInfoAccess>::index() as u8
		);
	}

	#[test]
	fn bridge_hub_inbound_v2_queue_pallet_index_is_correct() {
		assert_eq!(
			INBOUND_QUEUE_PALLET_INDEX_V2,
			<EthereumInboundQueueV2 as frame_support::traits::PalletInfoAccess>::index() as u8
		);
	}
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L254-287)
```rust
		let create_call_index: [u8; 2] = CreateAssetCall::get();
		let inbound_queue_pallet_index = InboundQueuePalletInstance::get();

		let xcm: Xcm<()> = vec![
			// Teleport required fees.
			ReceiveTeleportedAsset(total.into()),
			// Pay for execution.
			BuyExecution { fees: xcm_fee, weight_limit: Unlimited },
			// Fund the snowbridge sovereign with the required deposit for creation.
			DepositAsset { assets: Definite(deposit.into()), beneficiary: bridge_location.clone() },
			// This `SetAppendix` ensures that `xcm_fee` not spent by `Transact` will be
			// deposited to snowbridge sovereign, instead of being trapped, regardless of
			// `Transact` success or not.
			SetAppendix(Xcm(vec![
				RefundSurplus,
				DepositAsset { assets: AllCounted(1).into(), beneficiary: bridge_location },
			])),
			// Only our inbound-queue pallet is allowed to invoke `UniversalOrigin`.
			DescendOrigin(PalletInstance(inbound_queue_pallet_index).into()),
			// Change origin to the bridge.
			UniversalOrigin(GlobalConsensus(network)),
			// Call create_asset on foreign assets pallet.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: Some(Weight::from_parts(400_000_000, 8_000)),
				call: (
					create_call_index,
					asset_id,
					MultiAddress::<[u8; 32], ()>::Id(owner),
					MINIMUM_DEPOSIT,
				)
					.encode()
					.into(),
			},
```
