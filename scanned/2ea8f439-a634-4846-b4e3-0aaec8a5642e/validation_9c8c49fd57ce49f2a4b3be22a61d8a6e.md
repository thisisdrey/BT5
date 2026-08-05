### Title
Rotating `EthereumGatewayAddress` permanently strands `PendingOrders` and locks relayer rewards - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
The Snowbridge outbound queue (v2) binds delivery-receipt acceptance to a single global `T::GatewayAddress` value with no versioning or migration path when that value is rotated. This is the same "unclear upgrade path" defect described in the zAuction report: a reference to the currently-valid counterpart contract can be switched, but state created against the *old* reference is never migrated, drained, or made claimable again — it is simply orphaned. Unlike zAuction (where WETH bids could keep flowing to two live contracts), here the failure mode is a permanent fund/state lock: any message accepted by the old Gateway before rotation can never again be settled once the address changes, because the receipt’s `gateway` field is compared bit-for-bit against the live `GatewayAddress`.

### Finding Description
`snowbridge_pallet_outbound_queue_v2::Pallet::do_process_message` creates a `PendingOrder { nonce, fee, block_number }` for every outbound message and stores it keyed by `nonce`: [1](#0-0) 

The only way to resolve (and pay out) that order is `process_delivery_receipt`, which strictly requires the receipt’s `gateway` field to equal the *current* runtime value of `T::GatewayAddress`: [2](#0-1) 

`GatewayAddress` is not an immutable compile-time constant in practice — it is exposed as a governance-changeable storage constant, exactly as tested in the Bridge Hub Rococo test suite: [3](#0-2) 

This mirrors the zAuction pattern precisely: `zAuctionAccountant.SetZauction` lets an admin swap the address that the accountant checks against, and any state (bids) tied to the old address is instantly and permanently orphaned once the switch takes effect. Here, `EthereumGatewayAddress` plays the role of `zauction`: it is the single reference that `process_delivery_receipt` — the only public entry point capable of releasing a `PendingOrder` — checks against. There is no `(gateway_address, nonce)` compound key, no epoch/generation tag stored alongside `PendingOrder`, and no code path that migrates, replays, or refunds orders whose messages were committed against a since-rotated `GatewayAddress`.

Once the address is rotated (which is the intended, documented way to migrate to a new Gateway contract implementation on Ethereum, mirrored by `snowbridge_pallet_system_v2::Pallet::upgrade`'s `Command::Upgrade`), every outstanding `PendingOrder` created before the rotation becomes permanently unresolvable:
- Any relayer legitimately holding a valid delivery proof from the *old* Gateway contract will have `receipt.gateway != T::GatewayAddress::get()`, hitting `Error::<T>::InvalidGateway` forever.
- The corresponding `PendingOrder` entry, and the fee/tip value it represents, remains in storage with no code path to remove, refund, or reward it (`AddTip` only mutates existing entries; nothing purges orphaned ones).
- This is not a hypothetical governance-abuse scenario — the bug exists even when the rotation is executed exactly as intended by protocol operators, because the pallet itself provides no mechanism to drain or reconcile in-flight orders bound to the old address before/at the point of rotation.

### Impact Explanation
This falls squarely under "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" boundary conditions in the impact gate: relayer reward fees already committed and reserved for in-flight `PendingOrders` become permanently unclaimable once `GatewayAddress` is rotated, since the only public dispatchable that can resolve them (`submit_delivery_receipt` → `process_delivery_receipt`) hard-fails on the gateway-address equality check. There is no admin/root recovery call either (no `force_remove_pending_order` or similar), so the locked state and relayer compensation are irrecoverable without a further runtime code change/migration — i.e., without a bespoke intervention outside normal pallet operation.

### Likelihood Explanation
Gateway rotation is a designed, expected operational event (the entire purpose of `EthereumGatewayAddress` being governance-mutable and of `snowbridge_pallet_system_v2::upgrade` existing is to support Gateway contract upgrades on Ethereum). Any rotation performed while messages are in flight (which is essentially guaranteed given asynchronous relay delivery and the multi-block commit/relay/verify pipeline) will trigger this bug deterministically for every outstanding order, with no attacker action required beyond simply relaying a legitimate, valid proof after the rotation.

### Recommendation
Bind `PendingOrder` (and the receipt-matching logic) to the specific `GatewayAddress` value that was live at message-commit time, not the live runtime value at settlement time — e.g., store the originating gateway address (or an incrementing "gateway epoch") in `PendingOrder` and compare the receipt against that stored value rather than `T::GatewayAddress::get()`. Additionally, provide a governed migration/drain path executed atomically with any `GatewayAddress` rotation that settles, refunds, or explicitly voids all outstanding `PendingOrders` before the new address becomes authoritative, so no order is silently and permanently orphaned.

### Proof of Concept
1. Outbound queue commits message `M` with nonce `N`; `PendingOrders[N] = { fee: F, .. }` is stored (`do_process_message`).
2. Ethereum-side Gateway contract at address `A` accepts/executes `M`, emitting a receipt with `gateway = A`.
3. Before the relayer submits the delivery proof, governance rotates `EthereumGatewayAddress` from `A` to `B` (a normal, sanctioned upgrade/migration action, as covered by `change_ethereum_gateway_by_governance_works`).
4. Relayer calls `submit_delivery_receipt` with the valid, correctly-verified proof for `M` (`gateway = A`).
5. `process_delivery_receipt` executes `ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway)`; since `T::GatewayAddress::get() == B != A`, this fails with `InvalidGateway`.
6. `PendingOrders[N]` is never removed, and fee `F` is never paid to any relayer — it is permanently stuck, with no dispatchable in the pallet able to reach or resolve it.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L452-464)
```rust
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/tests/tests.rs (L216-229)
```rust
	#[test]
	fn change_ethereum_gateway_by_governance_works() {
		bridge_hub_test_utils::test_cases::change_storage_constant_by_governance_works::<
			Runtime,
			EthereumGatewayAddress,
			H160,
		>(
			collator_session_keys(),
			bp_bridge_hub_rococo::BRIDGE_HUB_ROCOCO_PARACHAIN_ID,
			Governance::get(),
			|| (EthereumGatewayAddress::key().to_vec(), EthereumGatewayAddress::get()),
			|_| [1; 20].into(),
		)
	}
```
