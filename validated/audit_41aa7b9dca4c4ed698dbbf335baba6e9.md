Confirmed. The code fully supports the claim.

**Verification of the burn-then-lose flow:**

1. `snowbridge_pallet_system_frontend::Pallet::add_tip` on AssetHub swaps the tip asset and burns the resulting ether via `swap_fee_asset_and_burn` → `burn_for_teleport` **before** dispatching the XCM `Transact` to BridgeHub: [1](#0-0) 

2. The burn itself happens inside `swap_fee_asset_and_burn`, irreversibly removing the asset from the user's account: [2](#0-1) 

3. On BridgeHub, `snowbridge_pallet_system_v2::Pallet::add_tip` forwards to `InboundQueue::add_tip`/`OutboundQueue::add_tip`; if that fails, it swallows the error, records it only in `LostTips`, and still returns `Ok(())`: [3](#0-2) 

4. `snowbridge-pallet-inbound-queue-v2::AddTip::add_tip` fails with `AddTipError::NonceConsumed` precisely when the nonce has already been processed by a relayer racing the tipper's transaction: [4](#0-3)  and message processing sets `Nonce::<T>::set(nonce)` in `process_message`: [5](#0-4) 

5. The `LostTips` storage item is documented in-code as informational only, "capturing the lost tips here supports implementing a recovery method **in the future**" — confirming no refund/claim extrinsic currently exists: [6](#0-5) 

6. The `pr_9746.prdoc` corroborates that this exact "burnt-then-lost" tip defect is a known, previously-occurring issue class in this exact code area (`snowbridge-pallet-inbound-queue-v2`): [7](#0-6) 

This closes the gap noted in the original claim (uncertainty about the frontend burn code) — the burn-before-forward flow is real and reachable by any signed account calling `system-frontend::add_tip`, racing ordinary relayer message submission. No privileged actor is required; only ordinary timing/ordering between a user's tip transaction and a relayer's `submit` call.

Audit Report

## Title
Snowbridge tip top-up permanently burns relayer-reward funds on `add_tip` failure with no refund path - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`, `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`snowbridge_pallet_system_frontend::Pallet::add_tip` on AssetHub swaps the user's tip asset for ether and permanently burns it via `burn_for_teleport` before dispatching an XCM `Transact` to `snowbridge_pallet_system_v2::Pallet::add_tip` on BridgeHub. If the corresponding `InboundQueue::add_tip`/`OutboundQueue::add_tip` call fails (e.g., `AddTipError::NonceConsumed` because a relayer already processed the message nonce), `system-v2::add_tip` swallows the error, records the amount into the purely informational `LostTips` storage map, and still returns `Ok(())` — permanently losing the user's already-burned funds with no restitution path.

## Finding Description
The value-burning step and the "processed" step are not atomic and are split across two different pallets/chains connected only by an unpaid XCM `Transact`. On AssetHub, `system-frontend::add_tip` burns the ether unconditionally as its first effect: [1](#0-0)  using `swap_fee_asset_and_burn`, whose `burn_for_teleport` call is irreversible: [2](#0-1) . The XCM `Transact` is then sent unpaid/best-effort to BridgeHub: [8](#0-7) .

On BridgeHub, `system-v2::add_tip` attempts to forward the tip to the message queue, but on any failure it only bookkeeps the loss into `LostTips` and returns `Ok(())` regardless: [3](#0-2) . The failure condition is reachable under ordinary conditions: `inbound-queue-v2::AddTip::add_tip` returns `AddTipError::NonceConsumed` whenever the message nonce was already marked processed: [4](#0-3) , and nonces are marked processed as part of ordinary relayer message submission via `process_message`: [5](#0-4) .

The only mitigation present is the `LostTips` storage map, whose own doc comment concedes there is currently no recovery extrinsic ("supports implementing a recovery method **in the future**"): [6](#0-5) . This is insufficient — it is a passive counter with no drain/claim path anywhere in the pallet.

## Impact Explanation
This breaks the invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Because the burn on AssetHub is unconditional and irrecoverable, and the BridgeHub-side failure is silently absorbed into `Ok(())` with only an informational counter, any unprivileged user calling `system-frontend::add_tip` for a message whose nonce races with relayer submission permanently loses real value (already-burned ether) that is never applied to any relayer reward and never refunded. This matches the "permanent user-fund lock" impact category in the Polkadot SDK impact gate.

## Likelihood Explanation
This requires no privileged actor, malicious relayer, or governance action — it is triggered by ordinary bridge traffic timing. Any signed account calling `system-frontend::add_tip` for `Inbound(nonce)`/`Outbound(nonce)` concurrently with, or shortly after, a relayer's `submit` finalizing that nonce (a normal race under live bridge traffic) hits this path. It is repeatable at will and does not depend on any race being deliberately engineered by an attacker against a victim (though a griefing relayer could also intentionally race to guarantee the loss for any observed tip transaction, since nonces and pending tips are public state).

## Recommendation
- Make `system-v2::add_tip` propagate the inner error and fail the extrinsic instead of returning `Ok(())` on failure, so the frontend/burn step and the queue-side bookkeeping fail atomically together (or so the caller can react).
- Restructure the flow so the AssetHub-side burn only occurs after confirmation that the BridgeHub-side `add_tip` succeeded (e.g., via a two-phase commit, or by moving the burn to occur on BridgeHub after `add_tip` succeeds), rather than burning first and forwarding second.
- Add an on-chain reimbursement/claim extrinsic keyed off `LostTips<T>` so affected accounts can recover already-burned funds when the forward step fails.

## Proof of Concept
1. A user calls `system-frontend::add_tip(origin, Inbound(nonce), tip_asset)` on AssetHub for a message not yet finalized on BridgeHub; this swaps and burns the tip amount `X` via `burn_for_teleport`, then sends an XCM `Transact` targeting `system-v2::add_tip`.
2. Concurrently, a relayer submits `EthereumInboundQueueV2::submit` for the same `nonce`, which calls `process_message`, setting `Nonce::<T>::set(nonce)`.
3. The XCM `Transact` executes `system-v2::add_tip(origin, sender, Inbound(nonce), X)` on BridgeHub, which calls `InboundQueue::add_tip(nonce, X)`, hitting `ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed)` and returning `Err(AddTipError::NonceConsumed)`.
4. `system-v2::add_tip` catches this error, adds `X` to `LostTips::<T>::get(sender)`, emits `TipProcessed { success: false }`, and returns `Ok(())`.
5. The user's `X` ether (already burned on AssetHub in step 1) is never applied to the relayer reward for `nonce` and is never refunded to the user; `LostTips` has no drain/claim extrinsic to recover it.

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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-142)
```rust
	/// Relayer reward tips that were paid by the user to incentivize the processing of their
	/// message, but then could not be added to their message reward (e.g. the nonce was already
	/// processed or their order could not be found). Capturing the lost tips here supports
	/// implementing a recovery method in the future.
	#[pallet::storage]
	pub type LostTips<T: Config> =
		StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L261-281)
```rust
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

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
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

**File:** prdoc/stable2509/pr_9746.prdoc (L1-12)
```text
title: Snowbridge Inbound Queue V2 relayer tip payout fix

doc:
- audience: Runtime Dev
  description: |
    Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been
    burnt.

crates:
- name: snowbridge-pallet-inbound-queue-v2
  bump: patch
- name: snowbridge-test-utils
```
