This is a strong local analog to the ResolvStaking bug: a pallet-level "halted" gate exists but is inconsistently enforced across the entry points that lead to the same payout effect.

### Title
`outbound-queue-v2::submit_delivery_receipt` pays relayer rewards with no pallet-level halt check, unlike sibling pallets that gate on `OperatingMode` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
In `pallet_inbound_queue_v2`, `submit` explicitly checks `OperatingMode::<T>::get().is_halted()` before any verification or state change occurs [1](#0-0) . `pallet_outbound_queue_v2` has no such pallet-level `OperatingMode` storage or check at all in `submit_delivery_receipt`; it relies entirely on `T::Verifier::verify` internally short-circuiting when the shared Ethereum client is halted [2](#0-1) . This mirrors the `ResolvStaking` bug: a "disabled" gate (`claimEnabled` / halt mode) protects one payout path (`claim` / `submit`) but a second payout-capable path (`withdraw` / `submit_delivery_receipt`+`process_delivery_receipt`) does not itself check the gate and only "happens" to be blocked by a side-effect of a different component (verifier halt) rather than an explicit, pallet-owned invariant.

### Finding Description
`Pallet::process_delivery_receipt` is a `pub fn` (not `pub(crate)`) that unconditionally pays out the relayer reward from `PendingOrders` once a `DeliveryReceipt` is supplied, with no `OperatingMode`/halt check of its own [3](#0-2) . The only thing preventing payout while the bridge is supposed to be halted is that `submit_delivery_receipt` calls `T::Verifier::verify` first, and the runtime's `EthereumBeaconClient::verify` was patched (per `prdoc/stable2603-2/pr_11856.prdoc`) to return `VerificationError::Halted` when the beacon client's own operating mode is halted [4](#0-3) .

This is a fragile, indirect enforcement rather than an explicit invariant in the outbound-queue-v2 pallet, exactly analogous to `ResolvStaking.withdraw()` not checking `claimEnabled` directly:
- The halt state lives in `pallet-ethereum-client`'s `OperatingMode`, not in `outbound-queue-v2`.
- `process_delivery_receipt` (the actual payout function) is `pub`, decoupled from the halt check, and reachable by any code path that can construct a `DeliveryReceipt` and call it directly (e.g., from a benchmark helper, a different call surface, or a future refactor that adds an alternate entry point), completely bypassing the `T::Verifier::verify` call that currently happens to enforce the halt.
- Unlike `inbound-queue-v2` and `pallet-ethereum-client`/`pallet-system`, which have their own `OperatingMode` storage and an explicit `ensure!(!OperatingMode::<T>::get().is_halted(), ...)` guard co-located with the dispatchable, `outbound-queue-v2` has none — there is no `Error::<T>::Halted` check reachable from `submit_delivery_receipt` at all; the pallet's own `Halted` error variant is defined but unused/unreachable in this pallet [5](#0-4) .

### Impact Explanation
If any future change decouples reward payout from the specific verifier used today (e.g., a benchmark/test helper wired into a runtime, a governance call that directly invokes `process_delivery_receipt`, or a refactor of the verifier that no longer maps halted-mode to `VerificationError::Halted`), relayer rewards can be paid out of `PendingOrders` while the bridge is meant to be halted — the same "duplicate settlement/payout despite disabled state" impact class called out as in-scope (bridge reward payout state advancing when it should be blocked). This is a public, unprivileged-relayer-triggerable payout path once the underlying gate is not enforced at the point of payout.

### Likelihood Explanation
Currently the halt is enforced *only* transitively through the shared `Verifier`, which is medium-likelihood to regress silently because (a) the pallet's own `Error::Halted` variant already exists but is dead code, signaling the intended-but-missing local check, and (b) `process_delivery_receipt` is `pub`, so it is not architecturally scoped to only be reachable after verification — nothing in the type system prevents another caller from invoking it directly.

### Recommendation
Add an explicit `OperatingMode` storage item and halt check directly in `pallet_outbound_queue_v2::submit_delivery_receipt` (and guard `process_delivery_receipt` itself, mirroring `inbound-queue-v2`'s `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)`), so the payout function does not depend solely on the verifier's incidental behavior to remain safe.

### Proof of Concept
Conceptual PoC (mirrors the `ResolvStaking` bypass): with the bridge halted, any caller with access to `Pallet::<T>::process_delivery_receipt` (bypassing `submit_delivery_receipt`/`T::Verifier::verify`) can supply a `DeliveryReceipt` for an existing `PendingOrders` entry and have `T::RewardPayment::register_reward` credit the reward, even though the intended halted state should block all bridge payout processing — the same call structure demonstrated by the existing `poc_m1` test in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` lines 390-416, but targeting `process_delivery_receipt` directly instead of going through the currently-patched `submit_delivery_receipt`/`Verifier::verify` path. [6](#0-5)

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-197)
```rust
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);

			// submit message for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into a bridge message
			let message =
				Message::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidMessage)?;

			Self::process_message(who, message)
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L225-243)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
		/// Invalid Channel
		InvalidChannel,
		/// Invalid Envelope
		InvalidEnvelope,
		/// Message verification error
		Verification(VerificationError),
		/// Invalid Gateway
		InvalidGateway,
		/// Pending nonce does not exist
		InvalidPendingNonce,
		/// Reward payment failed
		RewardPaymentFailed,
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
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

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-416)
```rust
// Reward processing must be blocked while the bridge is halted: `submit_delivery_receipt`
// should not pay out `PendingOrder` fees if the verifier reports the bridge as halted.
#[test]
fn poc_m1() {
	new_tester().execute_with(|| {
		let nonce = 1;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_event_proof());

		set_verifier_halted(true);

		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);

		let order_after = PendingOrders::<Test>::get(nonce).expect("order still present");
		assert_eq!(order_after.fee, fee);

		set_verifier_halted(false);
	});
}
```
