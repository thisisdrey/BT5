All code details in the claim check out exactly against the repository. Confirming key facts:

- `add_tip` on AssetHub (`bridges/snowbridge/pallets/system-frontend/src/lib.rs` L261-273) calls `swap_fee_asset_and_burn` which irreversibly burns/withdraws the user's asset via `swap_and_burn` (which itself calls `burn_for_teleport`, L290-317) or directly via `burn_for_teleport` (L399-401) *before* any XCM is sent — confirmed at [1](#0-0) .
- The resulting amount is packaged into an `EthereumSystemCall::AddTip` `Transact` and sent unpaid to BridgeHub, confirmed at [2](#0-1) .
- On BridgeHub, `pallet_snowbridge_system_v2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs` L251-281) attempts `InboundQueue::add_tip`/`OutboundQueue::add_tip`, and on any `Err` unconditionally mutates `LostTips::<T>` instead of refunding, confirmed at [3](#0-2) .
- `LostTips<T>` storage definition with the doc comment acknowledging no recovery method exists yet, confirmed at [4](#0-3) .
- `AddTipError::NonceConsumed`/`UnknownMessage` variants exist exactly as cited, confirmed at [5](#0-4) .
- A repo-wide search for `LostTips` shows it is only referenced in the storage definition/write path in `system-v2/src/lib.rs`, in `system-v2/src/tests.rs` (tests), and in an emulated integration test (`snowbridge_v2_outbound.rs`) — no dispatchable, hook, or governance call anywhere reads or drains `LostTips`, confirming there is genuinely no recovery path in the current pallet code.

Audit Report

## Title
Burned relayer-tip funds recorded in `LostTips` are permanently unclaimable - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

## Summary
`pallet-snowbridge-system-frontend::add_tip` irrevocably burns the user's real tip asset on AssetHub via `swap_fee_asset_and_burn`/`burn_for_teleport` before forwarding only the numeric amount via XCM `Transact` to `pallet-snowbridge-system-v2::add_tip` on BridgeHub. If crediting that amount to the target queue pallet fails (`AddTipError::NonceConsumed` or `UnknownMessage`), the amount is added to `LostTips<T>` instead of being refunded, and no extrinsic, hook, or governance call in the repository reads or drains `LostTips`, permanently locking the burned value.

## Finding Description
The flow is split across two chains with an irreversible action on the first leg and a fallible action on the second leg, with no compensation mechanism between them.

On AssetHub, `add_tip` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs` L261-273) calls `swap_fee_asset_and_burn`, which either swaps the asset to Ether and burns it, or burns Ether directly via `burn_for_teleport` — the user's funds are destroyed at this point regardless of what happens downstream, as seen in `swap_fee_asset_and_burn` (L372-404) and `swap_and_burn` (L290-317). The resulting `ether_gained` is packaged as `EthereumSystemCall::AddTip` and sent via unpaid `Transact` to BridgeHub (`build_add_tip_call`/`build_remote_xcm`, L340-363).

On BridgeHub, `pallet_snowbridge_system_v2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs` L251-281) calls `InboundQueue::add_tip`/`OutboundQueue::add_tip` for the given nonce. These can fail with `AddTipError::NonceConsumed` or `AddTipError::UnknownMessage` (`bridges/snowbridge/primitives/core/src/reward.rs` L32-37) if the referenced message nonce was already processed or doesn't exist. On any `Err`, the code unconditionally executes `LostTips::<T>::mutate(&sender, |lost_tip| *lost_tip = lost_tip.saturating_add(amount))` and still returns `Ok(())` from the dispatchable — there is no refund, no XCM sent back to AssetHub, and no alternate credit path.

The `LostTips` storage doc comment itself states the gap explicitly: "Capturing the lost tips here supports implementing a recovery method in the future" (L136-139) — but a repo-wide search confirms `LostTips` is referenced only in its storage definition and write path in `system-v2/src/lib.rs`, plus test files (`system-v2/src/tests.rs` and the emulated integration test `snowbridge_v2_outbound.rs`). The pallet's `#[pallet::call]` block only exposes `upgrade`, `set_operating_mode`, `register_token`, and `add_tip` — none of which reads back or redeems `LostTips`.

Because AssetHub's `add_tip` is signed-origin only (`ensure_signed(origin)?`) with no check that the referenced message nonce is still pending, any unprivileged user can trigger this by tipping a nonce that races against normal relayer message processing, or by referencing an already-consumed/unknown nonce, causing their already-burned funds to land in the unreadable `LostTips` map.

## Impact Explanation
This is a permanent user-fund lock: real Ether-denominated value is irreversibly burned on AssetHub as part of a legitimate tip flow, but the destination-side crediting on BridgeHub can fail for entirely benign and easily reachable reasons (nonce already consumed by ordinary relayer activity, or referencing an unknown/late nonce). Once that happens, the value is only reflected as a number in `LostTips<T>` with no code path anywhere in the pallet to move it back to a payable state — a direct, unprivileged loss of user funds in the Snowbridge tip/reward flow.

## Likelihood Explanation
Likelihood is realistic and requires no privileged or malicious behavior: `add_tip` and normal inbound/outbound message processing are independent transactions submitted by different, uncoordinated, honest actors (the tipper and the relayer/message-processing pipeline). Any user tipping a message nonce that gets processed concurrently, or referencing a stale/incorrect nonce, will hit this path purely from ordinary usage timing, making the issue repeatable without any adversarial coordination.

## Recommendation
Add a `claim_lost_tips`/`withdraw_lost_tips` extrinsic allowing the `sender` recorded in `LostTips<T>` to redeem their balance (e.g., via XCM teleport/mint back to their AssetHub account), or restructure `add_tip` to only burn/withdraw funds on AssetHub after BridgeHub confirms the crediting succeeded (e.g., an XCM query-response / two-phase commit pattern) rather than burning unconditionally up front.

## Proof of Concept
1. User A calls AssetHub `pallet_snowbridge_system_frontend::add_tip(message_id: Inbound(N), asset)`. The asset is burned immediately inside `swap_fee_asset_and_burn`.
2. Before or concurrently with the resulting XCM landing on BridgeHub, nonce `N` is consumed by normal message processing (independent of this attacker).
3. The XCM `Transact` carrying `EthereumSystemCall::AddTip { sender: A, message_id: Inbound(N), amount }` executes on BridgeHub, calling `InboundQueue::add_tip(N, amount)`, which returns `Err(AddTipError::NonceConsumed)`.
4. `pallet_snowbridge_system_v2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs` L266-271) catches the error and moves `amount` into `LostTips::<T>::mutate(&sender, ...)`, still returning `Ok(())` for the extrinsic.
5. Query `LostTips::<T>::get(A)` — it now holds the burned amount permanently, with no extrinsic in the pallet's `#[pallet::call]` block able to read or redeem it back to User A.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L340-363)
```rust
		// Build the call to dispatch the `EthereumSystem::add_tip` extrinsic on BH
		fn build_add_tip_call(
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> BridgeHubRuntime<T> {
			BridgeHubRuntime::EthereumSystem(EthereumSystemCall::AddTip {
				sender,
				message_id,
				amount,
			})
		}

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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
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

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L32-37)
```rust
#[derive(Debug, Encode, PartialEq, DecodeWithMemTracking, Decode, TypeInfo, PalletError)]
pub enum AddTipError {
	NonceConsumed,
	UnknownMessage,
	AmountZero,
}
```
