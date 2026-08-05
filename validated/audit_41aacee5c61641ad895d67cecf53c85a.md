Audit Report

## Title
Unvalidated message nonce in `AddTip::add_tip` permits burning user funds for a tip that can never be claimed - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

## Summary
`snowbridge_pallet_system_frontend::add_tip` on AssetHub irreversibly swaps/burns a caller-supplied asset for Ether and dispatches a Transact XCM carrying an arbitrary, caller-chosen `message_id`/`nonce` to `pallet_system_v2::add_tip` on BridgeHub, which forwards it to `pallet_inbound_queue_v2::AddTip::add_tip`. [1](#0-0)  That handler only checks `!Nonce::<T>::get(nonce)` (not yet consumed) before crediting `Tips::<T>`, with no verification that the nonce corresponds to a real, in-flight inbound message. [2](#0-1)  If the message never actually arrives (nonce picked too far ahead of the real Ethereum-driven sequence, or a nonce that will never be assigned), the burned Ether is permanently unrecoverable and the `Tips::<T>` entry persists indefinitely.

## Finding Description
`add_tip` in `system-frontend` is callable by any signed account with an arbitrary `message_id: MessageId` (an `Inbound(u64)`/`Outbound(u64)` nonce, per `snowbridge_core::reward::MessageId`) [3](#0-2) . It calls `Self::swap_fee_asset_and_burn`, which swaps the caller's asset for Ether and irreversibly burns it via `burn_for_teleport` [4](#0-3) , then dispatches `EthereumSystemCall::AddTip` to BridgeHub regardless of whether the nonce is valid [5](#0-4) .

On BridgeHub, `pallet_system_v2::add_tip` merely routes to `InboundQueue::add_tip(nonce, amount)` or `OutboundQueue::add_tip` based on the `MessageId` variant [6](#0-5) . `pallet_inbound_queue_v2::AddTip::add_tip` checks only `amount > 0` and `!Nonce::<T>::get(nonce)` (i.e., the nonce hasn't been consumed by a previously-processed message) before unconditionally crediting `Tips::<T>::mutate` [2](#0-1) . There is no registry or bound proving the nonce corresponds to a message that is queued, pending, or will ever be relayed from Ethereum — any `u64` not yet marked consumed is accepted.

`Tips::<T>` entries are only consumed inside `process_message`, called from `submit` when a genuinely verified message with that exact nonce arrives: `Tips::<T>::take(nonce)` adds the stored amount to the relayer reward [7](#0-6) . If no such message ever materializes for that nonce, the tip entry is never taken and the previously burned Ether is permanently stranded and unclaimable — this exactly matches the claimed root cause.

I also confirmed there is a partial mitigating mechanism on BridgeHub's `system-v2::add_tip`: if `InboundQueue::add_tip`/`OutboundQueue::add_tip` returns an error (e.g. `NonceConsumed`), the tip amount is tracked in `LostTips::<T>` for potential future recovery [8](#0-7) . However, `LostTips` is only populated when `add_tip` returns an `Err` (i.e., nonce already consumed) — the claim's exact scenario (a nonce that is *not yet* consumed but never will be, i.e. a future/phantom nonce) does not trigger this path at all, since `AddTip::add_tip` in `inbound-queue-v2` returns `Ok(())` for any unconsumed nonce. So the burned Ether is neither refunded via `LostTips` nor ever claimed via `Tips::<T>::take`.

## Impact Explanation
This is a genuine, reachable permanent-user-fund-loss condition triggerable by an ordinary unprivileged signed account with no dependency on relayer, prover, or governance misbehavior: value is irrevocably burned via `swap_fee_asset_and_burn`/`burn_for_teleport`, and the resulting `Tips::<T>` credit can become permanently unclaimable if the supplied nonce never corresponds to a real inbound message. This matches the "permanent user-fund lock" category in the impact gate.

## Likelihood Explanation
Likelihood is moderate rather than high: it requires the user (attacker or victim of their own error) to supply a nonce that does not currently exist and never will materialize into a real message (e.g., a far-future nonce beyond genuine sequential assignment from the Ethereum Gateway contract). Since nonce assignment is driven by the Ethereum side and there is no synchronous confirmation that a chosen nonce is "in flight," this is easy to trigger accidentally and also directly causes self-inflicted loss (there's no way to grief another user's funds this way — a caller can only burn their own assets). The finding stands as a valid design flaw whether triggered accidentally or intentionally by an unprivileged caller.

## Recommendation
Before crediting `Tips::<T>` in `pallet_inbound_queue_v2::AddTip::add_tip`, validate that `nonce` falls within a plausible pending window (e.g., track the highest nonce observed via `submit`/`process_message` and reject tips for nonces far beyond it, or maintain an explicit "expected/pending" nonce set). Additionally, extend the `LostTips`-style refund mechanism in `pallet_system_v2::add_tip` to also handle the "phantom nonce never consumed" case (e.g., via a deadline-based reclaim path), not just the "nonce already consumed" error case.

## Proof of Concept
1. On AssetHub, call `snowbridge_pallet_system_frontend::add_tip(message_id = MessageId::Inbound(nonce = <far-future or never-to-be-assigned value>), asset = <some fungible asset>)` as any signed account.
2. `swap_fee_asset_and_burn` swaps/burns the asset for Ether (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:372-404`); the pallet dispatches a Transact XCM to BridgeHub carrying `EthereumSystemCall::AddTip { sender, message_id, amount }`.
3. On BridgeHub, `pallet_system_v2::add_tip` routes to `pallet_inbound_queue_v2::AddTip::add_tip(nonce, amount)`, which checks only `!Nonce::<T>::get(nonce)` and stores `amount` in `Tips::<T>` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-258`) — returning `Ok(())`, so `LostTips` is not incremented.
4. Because the nonce was never actually assigned to a real Ethereum event, `submit`/`process_message` never fires for that nonce, so `Tips::<T>::take(nonce)` is never called (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:215-245`); the burned Ether from step 2 is permanently unrecoverable, and the `Tips::<T>` entry persists indefinitely with no cleanup or refund path.

### Citations

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-404)
```rust
		fn swap_fee_asset_and_burn(
			origin: Location,
			fee_asset: Asset,
		) -> Result<u128, DispatchError> {
			let ether_location = T::EthereumLocation::get();
			let (fee_asset_location, fee_amount) = match fee_asset {
				Asset { id: AssetId(ref loc), fun: Fungible(amount) } => (loc, amount),
				_ => {
					tracing::debug!(target: LOG_TARGET, ?fee_asset, "error matching fee asset");
					return Err(Error::<T>::UnsupportedAsset.into());
				},
			};
			if fee_amount == 0 {
				return Ok(0);
			}

			let ether_gained = if *fee_asset_location != ether_location {
				Self::swap_and_burn(
					origin.clone(),
					fee_asset_location.clone(),
					ether_location,
					fee_amount,
				)
				.inspect_err(|&e| {
					tracing::debug!(target: LOG_TARGET, ?e, "error swapping asset");
				})?
			} else {
				burn_for_teleport::<T::AssetTransactor>(&origin, &fee_asset)
					.map_err(|_| Error::<T>::BurnError)?;
				fee_amount
			};
			Ok(ether_gained)
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L215-245)
```rust
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L24-30)
```rust
#[derive(Debug, Clone, PartialEq, Encode, Decode, DecodeWithMemTracking, TypeInfo)]
pub enum MessageId {
	/// Message from Ethereum
	Inbound(u64),
	/// Message to Ethereum
	Outbound(u64),
}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-271)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}
```
