### Title
Missing `Halted` operating-mode check in `submit_delivery_receipt` allows continued reward payout while the Snowbridge outbound pipeline is supposed to be stopped - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
The external report's core defect is "a guard that exists in one function of a family is silently missing in a sibling function that should share the same invariant." The Snowbridge V2 bridge pallets contain a directly analogous inconsistency: `pallet-snowbridge-inbound-queue-v2` enforces an operating-mode halt check on every externally-callable message-processing entrypoint, but `pallet-snowbridge-outbound-queue-v2`'s `submit_delivery_receipt` — the sibling public entrypoint that pays relayer rewards — has no such check and no `OperatingMode` storage item at all.

### Finding Description
In `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`, the public `submit` call explicitly halts processing when the pallet is in a stopped state: [1](#0-0) 

This uses a dedicated `OperatingMode` storage value and `set_operating_mode` root call, giving governance a way to halt the pipeline (e.g. during an incident, bad verifier state, or fee-drain attack).

The sibling pallet, `pallet-snowbridge-outbound-queue-v2`, imports the same `BasicOperatingMode` type and even defines an `OperatingModeChanged` event, implying the same halt semantics were intended: [2](#0-1) [3](#0-2) 

However, the pallet's storage section defines only `Messages`, `MessageLeaves`, `Nonce`, and `PendingOrders` — there is no `OperatingMode` `StorageValue` and no `set_operating_mode` call: [4](#0-3) 

And the public `submit_delivery_receipt` extrinsic performs verification and reward payout with no halt/operating-mode gate whatsoever: [5](#0-4) 

The actual payout logic in `process_delivery_receipt` only checks the gateway address and pending-order existence before calling `T::RewardPayment::register_reward` and removing the order — again, no operating-mode gate: [6](#0-5) 

This is the same class of bug as the Audius `InitializableV2`/`_requireIsInitialized` report: a state-guarding check (`is_halted()`/`_requireIsInitialized()`) is applied consistently to one entrypoint of a contract family (`Governance.getVotingQuorum` / inbound `submit`) but is missing from a functionally equivalent sibling entrypoint (`Governance.getRegistryAddress` / outbound `submit_delivery_receipt`), with no code comment explaining the asymmetry.

### Impact Explanation
Because `submit_delivery_receipt` has no halt gate, if operators ever need to stop the outbound/reward-processing side of the bridge (the only mechanism visible for this is the inbound `set_operating_mode`, which does not affect the outbound pallet's separate storage), an unprivileged relayer can continue to call `submit_delivery_receipt` for any still-pending order, triggering `T::RewardPayment::register_reward` and draining `PendingOrders` fees regardless of the intended halt. This falls under "public underpriced work that degrades block production or stalls bridge processing" / continued payout processing that should have stopped, since the pallet provides no way to pause this state-mutating, fee-paying public entrypoint independent of governance intervening at a lower level (e.g., filtering the call in the runtime's `BaseCallFilter`, which is an external, non-pallet-local mitigation).

### Likelihood Explanation
High likelihood of triggerability: `submit_delivery_receipt` is a normal signed extrinsic reachable by anyone holding a valid Ethereum event proof for an already-queued order; no privileged action is needed to exploit the missing guard — only the absence of the pause mechanism that the pallet's own event/type definitions suggest was intended to exist.

### Recommendation
Add an `OperatingMode` storage item and `set_operating_mode` root call to `pallet-snowbridge-outbound-queue-v2` mirroring `pallet-snowbridge-inbound-queue-v2`, and gate both `submit_delivery_receipt` (and ideally `do_process_message`) with `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)`, matching the check already present in the inbound pallet.

### Proof of Concept
1. Assume governance wants to halt the Snowbridge V2 outbound reward pipeline (e.g., due to a discovered issue in `T::Verifier` or a fee-drain incident).
2. There is no call in `pallet-snowbridge-outbound-queue-v2` to halt it — `OperatingMode` storage/`set_operating_mode` simply doesn't exist there (confirmed by the pallet's storage list at `outbound-queue-v2/src/lib.rs:245-271`, contrast with `inbound-queue-v2/src/lib.rs:170-172,185-187`).
3. A relayer with a valid Ethereum event proof for any `PendingOrders` entry can call `submit_delivery_receipt` at any time; `process_delivery_receipt` (`outbound-queue-v2/src/lib.rs:446-480`) will still verify the gateway address, pay the reward via `T::RewardPayment::register_reward`, and remove the pending order — with no way for the pallet itself to stop this, unlike the inbound side which can be halted via `Error::<T>::Halted`.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-198)
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
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L81-85)
```rust
use snowbridge_core::{
	digest_item::SnowbridgeDigestItem,
	reward::{AddTip, AddTipError},
	BasicOperatingMode,
};
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L219-223)
```rust
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
		/// Delivery Proof received
		MessageDelivered { nonce: u64 },
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L245-271)
```rust
	/// Messages to be committed in the current block. This storage value is killed in
	/// `on_initialize`, so will not end up bloating state.
	///
	/// Is never read in the runtime, only by offchain message relayers.
	/// Because of this, it will never go into the PoV of a block.
	///
	/// Inspired by the `frame_system::Pallet::Events` storage value
	#[pallet::storage]
	#[pallet::unbounded]
	pub type Messages<T: Config> = StorageValue<_, Vec<OutboundMessage>, ValueQuery>;

	/// Hashes of the ABI-encoded messages in the [`Messages`] storage value. Used to generate a
	/// merkle root during `on_finalize`. This storage value is killed in `on_initialize`, so state
	/// at each block contains only root hash of messages processed in that block. This also means
	/// it doesn't have to be included in PoV.
	#[pallet::storage]
	#[pallet::unbounded]
	pub type MessageLeaves<T: Config> = StorageValue<_, Vec<H256>, ValueQuery>;

	/// The current nonce for the messages
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;

	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-318)
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
