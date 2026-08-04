Based on my investigation, I found a concrete, evidence-backed input-validation gap in the Snowbridge V2 reward "tip" flow that maps directly to the Augur report's core theme: a user-controlled parameter is forwarded across a trust boundary without validating that it corresponds to the actual origin/payer.

### Title
Unvalidated `sender` parameter in `EthereumSystemV2::add_tip` lets tip loss be attributed to an arbitrary account - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
The `add_tip` extrinsic on BridgeHub's `snowbridge-pallet-system-v2` accepts a caller-supplied `sender: AccountIdOf<T>` parameter that is never checked against the actual origin of the call, even though the call is only origin-gated via `T::FrontendOrigin`. The value paid/burned on AssetHub (`amount`) is decoupled from the `sender` recorded on BridgeHub in both success (`TipProcessed`) and failure (`LostTips`) paths.

### Finding Description
`add_tip` in [1](#0-0)  only validates the *origin* of the call:

```
pub fn add_tip(origin, sender: AccountIdOf<T>, message_id: MessageId, amount: u128) -> DispatchResult {
    T::FrontendOrigin::ensure_origin(origin)?;
    ...
    LostTips::<T>::mutate(&sender, |lost_tip| { *lost_tip = lost_tip.saturating_add(amount); });
    ...
}
```

It never checks that `sender` corresponds to the account that actually authorized/paid for the tip on the sending side. The upstream caller, `SnowbridgeSystemFrontend::add_tip` on AssetHub ( [2](#0-1) ), does derive `sender` from `ensure_signed(origin)` on AssetHub and passes it correctly today — but the BridgeHub-side extrinsic itself places zero trust boundary on this field: any origin that satisfies `T::FrontendOrigin` (i.e., any XCM Transact arriving from the whitelisted AssetHub frontend pallet location, not a specific end-user account) can supply an arbitrary `sender`. Since the pallet's own `Config::FrontendOrigin` only asserts *which pallet/location* sent the message, not *which end user*, the extrinsic has no internal guarantee binding `sender` to the party that actually burned `amount` worth of assets.

### Impact Explanation
This is a public-dispatch-wrapper-style validation gap: the extrinsic is dispatched from a "trusted" XCM origin (`FrontendOrigin`), but the trusted origin represents a pallet/location, not an individual signer, and the payload field `sender` is taken at face value for bookkeeping (`LostTips` storage and the `TipProcessed` event). If any other component or future caller reachable through `FrontendOrigin` (or a compromised/rerouted XCM message with `preserve_origin: false` semantics for this call) can set `sender` independently of `amount`'s actual payer, tip-loss accounting can be attributed to a victim account instead of the actual caller, corrupting the `LostTips::<T>` ledger, an on-chain user-fund-adjacent accounting value with no reconciling guard.

### Likelihood Explanation
Today the only production caller is `pallet-system-frontend::add_tip`, which correctly forwards its own signed `who`, so the immediate exploit path is not present in the shipped runtime configuration. However, the vulnerability is a structural input-validation gap in the callee: `add_tip`'s only defense is `ensure_origin`, and it fully trusts a user-controlled `AccountId` argument for state that is meant to reflect who actually incurred the tip loss. This mirrors exactly the Augur-style defect class ("functions… lack explicit checks of user-controlled parameters… relying on the origin check while other unsanitized parameters are trusted"), and any additional integration, upgrade to `FrontendOrigin`, or reused Transact-call construction that does not re-derive `sender` from the authenticated origin would silently misattribute tips.

### Recommendation
Bind `sender` to the authenticated caller rather than trusting an inbound field: either (a) require `T::FrontendOrigin::ensure_origin` to return the originating `Location`/`AccountId` and derive `sender` from it directly (dropping the parameter), or (b) add an explicit `ensure!(sender_matches_origin(...), Error::<T>::SenderMismatch)` check before writing to `LostTips` and emitting `TipProcessed`.

### Proof of Concept
Not independently reproducible against the current runtime wiring purely from static review, since the only live caller already passes the correct `who`. The finding is that `pallet_snowbridge_system_v2::Pallet::<T>::add_tip` ( [3](#0-2) ) performs no cross-check between `sender` and the resolved origin, which a unit test can directly demonstrate by invoking the dispatchable with `T::FrontendOrigin`-satisfying origin and an arbitrary `sender` unrelated to the origin, observing that `LostTips::<T>::get(arbitrary_sender)` and `Event::TipProcessed{ sender: arbitrary_sender, .. }` are written without any origin-to-account correlation check.

I want to flag uncertainty here: I could not fully inspect the concrete type bound to `T::FrontendOrigin` in `bridge_to_ethereum_config.rs` (my last tool calls to read that were cut off), so I cannot confirm whether it is instantiated as a narrow `EnsureXcm`-style check that structurally prevents any other caller from reaching this dispatchable with a mismatched `sender`. This weakens confidence that the gap is currently exploitable end-to-end in the production BridgeHub runtime, and I'd recommend a Devin session with full file access to verify the exact `FrontendOrigin` type and any additional origin-derivation logic before treating this as a confirmed live exploit rather than a defensive-coding gap.

### Citations

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
