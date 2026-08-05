## Title
Relayer tips permanently lost with no recovery path when `add_tip` targets a stale/invalid nonce - `LostTips` accounting is a dead end - (File: bridges/snowbridge/pallets/system-v2/src/lib.rs)

## Summary
The Snowbridge V2 tip mechanism mirrors exactly the pattern flagged in the external report: value is withdrawn from a user to fund a fee/tip, but if the corresponding accounting fails to attach that value to a real reward, the funds are recorded only in a bookkeeping map with no extrinsic ever provided to reclaim them. In `pallet_system_v2::add_tip`, when `InboundQueue::add_tip`/`OutboundQueue::add_tip` fails (nonce already consumed or pending order not found), the tip amount is added to `LostTips<T>` and the call still returns `Ok(())` — but there is no call in the codebase that reads and pays out from `LostTips`.

## Finding Description
`pallet_system_v2::Pallet::add_tip` is a public, unprivileged (`FrontendOrigin`) entrypoint reachable from `pallet_system_frontend` via XCM `Transact`/frontend calls on Asset Hub. It forwards the tip attempt to either the inbound or outbound queue's `AddTip::add_tip`: [1](#0-0) 

If that inner call fails (e.g., `AddTipError::NonceConsumed` in the inbound queue, or "pending order not found" in the outbound queue), the code swallows the error, unconditionally credits the amount to `LostTips<T>` keyed by `sender`, emits `TipProcessed { success: false, .. }`, and still returns `Ok(())`: [2](#0-1) 

The storage doc comment itself admits the funds are dead-ended: *"Capturing the lost tips here supports implementing a recovery method in the future."* No such recovery method (extrinsic, migration, or automated sweep) exists anywhere in the pallet, in `pallet_system_frontend`, or elsewhere in the bridge pallets that were inspected. The underlying asset transfer that produced this tip amount (the DOT/ETH withdrawn from the user via XCM in `pallet_system_frontend`'s add-tip flow) has already been executed/settled on-chain by the time this map is populated — the value has left the user's control and is not held anywhere retrievable by that user.

This is structurally identical to the primitive-protocol `Option.exercise` bug: a fee/value is computed and taken from the caller, but the contract logic does not keep a claimable record connecting the fee to a payee — it just updates internal bookkeeping that nobody can later act on.

Existing guards do not stop this path:
- `FrontendOrigin::ensure_origin` only checks the origin is a legitimate frontend/XCM origin — it does not validate the nonce exists before allowing the transfer/tip to proceed.
- The inner `AddTip::add_tip` implementations (`pallet_inbound_queue_v2::AddTip::add_tip`, outbound queue's equivalent) correctly reject invalid/consumed nonces, but the caller (`system_v2::add_tip`) treats this as merely "recorded," not "failed," and always returns success to the extrinsic caller.
- There's no re-entrant claim call, no root/governance sweep call found in this pallet, and no check preventing a user (or an attacker submitting spam tips) from repeatedly directing tips at already-consumed or forged nonces, permanently funneling funds into an inert map for potentially many different `sender` keys.

## Impact Explanation
This falls under "public underpriced work that degrades... or permanent user-fund... lock" and "theft or unbacked... loss" categories of the impact gate: funds paid by ordinary parachain/XCM users to incentivize relaying are permanently stranded once a race (or malicious/careless targeting of a stale nonce) occurs, with the runtime itself providing no mechanism to return them. Because `add_tip` is a normal, permissionless (any signed account routed through `FrontendOrigin`) call and the failure path is silently converted into a successful dispatch (`Ok(())`), users have no on-chain signal beyond an event to detect that they must intervene, and even if they do notice, there is nothing to call. Given Snowbridge's tip amounts can be arbitrarily sized (bounded only by what the frontend allows the user to attach), this can lock non-trivial value with no recovery.

## Likelihood Explanation
Likelihood is moderate-to-high in practice: tips are frequently added *after* a message is already submitted/relayed (race between the relayer processing a message and the user/agent submitting a tip for it), which is exactly the "nonce already consumed" case explicitly tested in `add_tip_inbound_fails_when_nonce_is_consumed` and `add_tip_outbound_fails_when_pending_order_not_found`. This is not a contrived edge case — it's a documented, tested failure mode of the normal tipping workflow, meaning under regular operation (not requiring a malicious relayer/validator/governance actor) legitimate users will periodically lose tip funds with no path to recovery.

## Recommendation
Provide an actual, callable recovery mechanism for `LostTips`, e.g., a signed extrinsic `claim_lost_tip(origin)` that reads and zeroes `LostTips::<T>::take(&who)` and transfers/mints the corresponding value back to the caller (or via the reward pot mechanism used elsewhere in the bridge). Alternatively, reject the tip transfer itself before committing to `LostTips` (i.e., validate nonce/order existence prior to withdrawing funds from the user), so that a failed tip attempt never actually debits the user in the first place. At minimum, `add_tip` should not return `Ok(())` on the failure branch if funds were genuinely already taken and are unrecoverable, and the pallet documentation strings should not describe an aspirational "future recovery method" that does not exist and has no tracked implementation.

## Proof of Concept
Using the existing pallet test harness (no modification needed to demonstrate the dead-end):
1. Call `pallet_system_v2::add_tip(FrontendOrigin, sender, MessageId::Inbound(FAILING_NONCE), amount)` where `FAILING_NONCE` corresponds to an already-processed/consumed nonce.
2. Observe (as validated in the existing test `add_tip_inbound_fails_when_nonce_is_consumed`) that the call succeeds (`assert_ok!`), a `TipProcessed { success: false }` event fires, and `LostTips::<Test>::get(sender) == amount`: [3](#0-2) 
3. Search the full `pallet_system_v2`, `pallet_system_frontend`, and related bridge-hub runtime code for any extrinsic, hook, or migration that reads from `LostTips` to pay it out — none exists. The value recorded at step 2 is permanently unreachable by the `sender` account through any on-chain call path currently in the repository.

### Citations

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

**File:** bridges/snowbridge/pallets/system-v2/src/tests.rs (L197-219)
```rust
#[test]
fn add_tip_inbound_fails_when_nonce_is_consumed() {
	new_test_ext(true).execute_with(|| {
		let origin = make_xcm_origin(FrontendLocation::get());
		let sender: AccountId = Keyring::Alice.into();
		// In `MockOkInboundQueue`, the mocked implementation returns an error when the nonce is
		// equal to 3, to simulate an error condition.
		let message_id = MessageId::Inbound(FAILING_NONCE);
		let amount = 1000;

		assert_ok!(EthereumSystemV2::add_tip(origin, sender.clone(), message_id.clone(), amount));

		System::assert_last_event(RuntimeEvent::EthereumSystemV2(Event::<Test>::TipProcessed {
			sender: sender.clone(),
			message_id,
			amount,
			success: false,
		}));

		let lost_tip = LostTips::<Test>::get(sender);
		assert_eq!(lost_tip, 1000);
	});
}
```
