## Title
Outbound queue v2 continues to accrue/settle relayer fees while its own `Halted` operating mode never actually exists — `set_operating_mode` is missing from `submit_delivery_receipt`'s gating path - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
The `pallet-snowbridge-outbound-queue-v2` module declares an `Error::Halted` variant and an `Event::OperatingModeChanged` event, both artifacts of a `BasicOperatingMode` halt mechanism, yet the pallet defines **no `OperatingMode` storage item and no `set_operating_mode` extrinsic** of its own. `submit_delivery_receipt` — the only gate that removes a `PendingOrder` and pays out the relayer fee — never checks any local halt flag; it relies exclusively on `T::Verifier::verify()` (the `pallet-ethereum-client`) returning `VerificationError::Halted`. Meanwhile `do_process_message` (invoked by the `MessageQueue` for every outbound message) keeps creating new `PendingOrder`s with fees attached regardless of any halt state, and `AddTip::add_tip` keeps allowing fee top-ups on existing pending orders. This mirrors the reported pattern precisely: the “interest/fee accrual” path (`do_process_message`, `add_tip`) is completely unguarded by any pause mechanism specific to this pallet, while the “settlement/repay” path (`submit_delivery_receipt`) is only indirectly gated through a *different* pallet's halt flag — a fragile, single point of dependence rather than the pallet's own authoritative halt state.

## Finding Description
`pallet-outbound-queue-v2`'s call section only exposes: [1](#0-0) 

There is no `OperatingMode` storage value and no `set_operating_mode` call in this pallet, despite the `Error<T>::Halted` and `Event::OperatingModeChanged` variants existing in the pallet's own error/event enums: [2](#0-1) 

`submit_delivery_receipt` pays the relayer fee purely by delegating verification to `T::Verifier`: [3](#0-2) 

The only reason a halt currently blocks this path is that `pallet-ethereum-client`'s `Verifier::verify` was patched to check its own halted flag: [4](#0-3) 

Crucially, `do_process_message` — the function that creates each new `PendingOrder` (the fee obligation that later gets paid out) — has **no halt check at all**, and neither does `AddTip::add_tip`, which can top up the fee on an existing pending order: [5](#0-4) [6](#0-5) 

This is structurally identical to the VaultController bug: the accrual side (`do_process_message`/`add_tip`, analogous to `pay_interest()`) has no pause awareness whatsoever, while the settlement side (`submit_delivery_receipt`, analogous to `repayUSDi()`) is only protected as a side effect of a *different* module's (`pallet-ethereum-client`) halt check being routed through the shared `Verifier` trait. If any runtime wires a `Verifier` implementation for outbound-queue-v2 that does not itself check a halted state (e.g., a test/mocked verifier, or a future alternate light client backend that omits the halt guard), `submit_delivery_receipt` would have zero protection — there is no pallet-local `ensure!(!Self::operating_mode().is_halted(), ...)` as a defense-in-depth. This is exactly the anti-pattern the original report calls out: making the pause behavior of one function a downstream consequence of an unrelated computation/verification path (here, an external `Verifier` implementation) rather than an explicit, pallet-owned invariant, "any issue with [that] calculation could potentially render the [halt] unstoppable" — or in this case, non-existent.

## Impact Explanation
If the `Verifier` binding used for outbound-queue-v2 in a given runtime does not itself enforce a halt (as is the case for `MockVerifier` in tests, and would be the case for any alternative light-client/verifier swapped in later), governance's `set_operating_mode` on `pallet-ethereum-client` provides no actual protection for `submit_delivery_receipt`, `do_process_message`, or `add_tip` in `outbound-queue-v2`. New `PendingOrder`s keep being created and fees keep being augmentable via `add_tip` even during an emergency halt, and reward payouts via `submit_delivery_receipt` continue unguarded — undermining the "halt the bridge" governance action for outbound settlement, potentially draining reward funds or allowing continued processing during a period meant to be frozen (e.g., a compromised beacon light client scenario), which is exactly the risk `pr_11856.prdoc` was meant to close for the currently-wired configuration.

## Likelihood Explanation
Moderate. In the current default runtime wiring, the fix in `pallet-ethereum-client::Verifier::verify` (checking its own halted state) happens to protect `submit_delivery_receipt` as a side effect. But this protection is not pallet-owned, not tested in `outbound-queue-v2`'s own test suite against a "real" verifier removal, and not enforced for `do_process_message`/`add_tip` at all. Any refactor, alternate build configuration, or future verifier implementation that omits the halt check (as the bundled `MockVerifier` in `outbound-queue-v2`'s own test mock does — it only reports `Halted` via a thread-local test hook, not a genuine `OperatingMode` storage) reintroduces the exact accrual-during-pause bug described in the report.

## Recommendation
Give `pallet-outbound-queue-v2` its own authoritative `OperatingMode` storage item and `set_operating_mode` extrinsic (as `inbound-queue-v2` and `pallet-ethereum-client` already have), and add an explicit `ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted)` check at the top of `submit_delivery_receipt`, `do_process_message`, and `AddTip::add_tip`, rather than relying solely on the `Verifier` implementation to transitively enforce a halt for an unrelated pallet. This removes the single point of failure and matches the report's core recommendation: do not make the halt behavior of one control path a downstream consequence of another module's internal computation.

## Proof of Concept
1. Configure a runtime (or test harness) where `outbound-queue-v2::Config::Verifier` is bound to any verifier implementation that does not check a halted flag (e.g., the pallet's own `MockVerifier` used in `mock.rs`, which only returns `Halted` via an explicit `set_verifier_halted(true)` test hook rather than real `OperatingMode` storage) — see [7](#0-6) .
2. Governance calls `pallet-ethereum-client::set_operating_mode(Halted)` intending to freeze all bridge operations.
3. Because `outbound-queue-v2` has no `OperatingMode` storage/extrinsic of its own, and its bound `Verifier` in this configuration does not consult `pallet-ethereum-client`'s halted flag, `do_process_message` keeps enqueuing new `PendingOrder`s with fees, `AddTip::add_tip` keeps topping up fees on pending orders, and `submit_delivery_receipt` keeps paying relayer rewards from `PendingOrders` — none of which observe the halt.
4. This reproduces the underlying "accrual continues, only the unrelated repay path was blocked as a side effect" bug class from the external report, but shows the outbound-queue-v2 pallet has no independent, defense-in-depth halt of its own.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L219-243)
```rust
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
		/// Delivery Proof received
		MessageDelivered { nonce: u64 },
	}

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-317)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: From<[u8; 32]>,
	{
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L15-41)
```rust
impl<T: Config> Verifier for Pallet<T> {
	/// Verify a message by verifying the existence of the corresponding
	/// Ethereum log in a block. Returns the log if successful. The execution header containing
	/// the log is sent with the message. The beacon header containing the execution header
	/// is also sent with the message, to check if the header is an ancestor of a finalized
	/// header.
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;

		Self::verify_receipt_inclusion(
			proof.execution_proof.execution_header.receipts_root(),
			event_log.tx_index,
			&proof.receipt_proof,
			event_log,
		)?;

		Ok(())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/mock.rs (L74-92)
```rust
// Mock verifier
pub struct MockVerifier;

std::thread_local! {
	static VERIFIER_HALTED: core::cell::Cell<bool> = const { core::cell::Cell::new(false) };
}

pub fn set_verifier_halted(halted: bool) {
	VERIFIER_HALTED.with(|v| v.set(halted));
}

impl Verifier for MockVerifier {
	fn verify(_: &Log, _: &Proof) -> Result<(), VerificationError> {
		if VERIFIER_HALTED.with(|v| v.get()) {
			return Err(VerificationError::Halted);
		}
		Ok(())
	}
}
```
