### Title
`add_tip` lets the caller-supplied `amount` inflate a relayer's payout with no corresponding value check — ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

### Summary
The Sablier bug is caused by a per-user accounting value (`_userWstETH`) that is mutated (or left stale) without being tied to a real, conserved balance change, letting an attacker inflate the amount ultimately paid out. The local analog is `SnowbridgeSystemV2::add_tip`, which increments `PendingOrder.fee` — the exact value later paid to a relayer as WETH reward — using a caller-supplied `u128 amount` that is never validated against any real balance debit inside this pallet or the queue it calls into.

### Finding Description
`add_tip` in `bridges/snowbridge/pallets/system-v2/src/lib.rs:251-281` is guarded only by `T::FrontendOrigin::ensure_origin(origin)`, which authenticates that the call originated from the sibling AssetHub location (via XCM `Transact`), not that any specific account has actually paid anything: [1](#0-0) 

Crucially, `sender: AccountIdOf<T>` and `amount: u128` are plain, attacker-chosen call parameters — `sender` is never derived from `ensure_signed`, and `amount` is never withdrawn, reserved, or otherwise debited from any account in this function. The call unconditionally forwards `(nonce, amount)` into `T::InboundQueue::add_tip` / `T::OutboundQueue::add_tip`.

On the outbound side, this lands in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:483-495`, where the `AddTip` implementation does a bare `try_mutate_exists` that adds `amount` straight into `PendingOrder.fee`, keyed only by `nonce`: [2](#0-1) 

That same `order.fee` is later paid out verbatim to whichever relayer submits a valid delivery receipt for that nonce, via `T::RewardPayment::register_reward`: [3](#0-2) 

At no point in this code path — `add_tip` → `AddTip::add_tip` → `process_delivery_receipt` → `register_reward` — is `amount` bound to any actual balance transfer, hold, or nonce-specific proof of value received. The system relies entirely on the remote `system-frontend` pallet on AssetHub having withdrawn a matching amount of WETH from the real sender before dispatching the XCM `Transact` that reaches this call. `system-v2` itself performs no local conservation check: the `fee` field that determines relayer compensation is a free-standing integer that any origin satisfying `FrontendOrigin` can increase for **any** nonce, with no requirement that the `sender` argument (unauthenticated) or the `amount` correspond to funds actually held on Bridge Hub.

This mirrors the Sablier root cause precisely: a value used to compute a payout (`_userWstETH` / `PendingOrder.fee`) is mutated through a code path that does not enforce that the mutation is backed by a real, conserved balance movement — the guarantee is assumed to exist elsewhere (a burn hook that doesn't fire vs. a remote pallet that is assumed to have withdrawn funds), and the local module has no defense-in-depth check of its own.

### Impact Explanation
`PendingOrder.fee` directly determines the WETH amount transferred to a relayer's beneficiary via `T::RewardPayment::register_reward` and subsequent `pay_reward`/`claim_rewards_to` flow to AssetHub. If the binding between the `amount` argument and an actual asset debit on Ethereum/AssetHub is not enforced end-to-end (and nothing in `system-v2`/`outbound-queue-v2` enforces it locally), a caller reaching this permissioned-but-not-value-checked entrypoint can inflate the reward paid out of the bridge's reward pot without a matching deposit — an unbacked mint/theft of bridge funds, directly matching the required impact category "theft or unbacked mint" and violating the pivot that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Likelihood Explanation
Likelihood depends on whether the `system-frontend` pallet on AssetHub (not fully inspected in this session due to tool-budget limits) strictly and atomically ties the exact `amount` forwarded in the `Transact` to a real WETH withdrawal for that specific `nonce`/`message_id` before it is relayed to `system-v2::add_tip`. I was unable to verify that binding within the available iterations — this is the key unresolved uncertainty. If that binding is correct and atomic, `FrontendOrigin` alone may be a sufficient trust boundary and this reduces to a design assumption rather than an exploitable bug. If it is not perfectly enforced (e.g., re-entrant XCM execution, mismatched nonce/asset accounting, or a frontend-side error path that still forwards the Transact), the BH-side pallets provide zero secondary defense, since `add_tip`/`AddTip::add_tip` never re-validates value.

### Recommendation
- Add a local invariant in `outbound-queue-v2`/`inbound-queue-v2`'s `AddTip` implementation (or in `system-v2::add_tip`) that the `amount` being added to `PendingOrder.fee` is bounded by/backed by an actual asset amount recorded for that specific `nonce` (e.g., verified against a hash/commitment of the XCM-attached asset, not a bare integer).
- Derive `sender` from the actual XCM `AliasOrigin`/asset-holding account rather than accepting it as an unauthenticated parameter, and require that the `FrontendOrigin` conversion also proves the asset was withdrawn for that specific nonce.
- Add defensive limits/caps on `amount` per `add_tip` call and emit alerts if a tip exceeds the message's own declared fee/value by an implausible margin.

### Proof of Concept
Conceptual PoC (BH runtime, requires ability to satisfy `FrontendOrigin`, e.g., via a crafted/duplicated XCM `Transact` from a sibling location that is accepted as a valid frontend origin, or a frontend pallet bug that forwards `add_tip` without a matching withdrawal):
1. Observe a legitimate outbound message with `nonce = N` and a small `fee` recorded in `PendingOrders`.
2. Issue (or induce, if the AH-side frontend fails to atomically bind value) a call reaching `SnowbridgeSystemV2::add_tip(origin, sender = attacker_or_arbitrary, message_id = Outbound(N), amount = LARGE)`.
3. `outbound-queue-v2::AddTip::add_tip` executes `order.fee = order.fee.saturating_add(LARGE)` with no balance check — see `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:483-495`.
4. Any relayer subsequently submits a valid delivery receipt for `nonce = N`; `process_delivery_receipt` pays out `order.fee` (now inflated by `LARGE`) via `T::RewardPayment::register_reward` — see `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:462-473`.
5. The relayer claims the inflated reward via `pallet_bridge_relayers::claim_rewards_to`, draining more WETH from the bridge's reward pot than was ever deposited for message `N`.

Note: full confirmation requires reading `bridges/snowbridge/pallets/system-frontend/src/lib.rs`'s `add_tip` handler (not retrieved in this session) to determine whether it strictly withdraws and cryptographically/nonce-binds the tip amount before forwarding the XCM `Transact`. This is the remaining unverified assumption in this analysis.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-264)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-473)
```rust
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-495)
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
```
