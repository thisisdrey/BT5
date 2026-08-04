## Summary

The Revert Lend bug pegs a liquidator's incentive fee to a *shrinking* value (`fullValue`) instead of the *fixed* target it is supposed to cover (`debt`), so the incentive to act erodes exactly when action is most urgently needed. The same broken pattern — reward computed as `min(available_funding_source, fixed_target_cost)` instead of always paying the fixed target — exists in the Snowbridge inbound-queue relayer-reward logic.

## Title
Relayer reward for delivering Ethereum→Polkadot messages is capped by the sovereign account's shrinking balance instead of the fixed delivery cost, disincentivizing relaying and stalling bridge processing - (File: `bridges/snowbridge/pallets/inbound-queue/src/lib.rs`)

## Finding Description
`submit()` in `Pallet::<T>::submit` computes the fixed cost of delivering a message via `Self::calculate_delivery_cost(...)`, then computes the actual amount paid to the relayer as:

```rust
let sovereign_account = sibling_sovereign_account::<T>(channel.para_id);
let delivery_cost = Self::calculate_delivery_cost(event.encode().len() as u32);
let amount = T::Token::reducible_balance(
    &sovereign_account,
    Preservation::Preserve,
    Fortitude::Polite,
)
.min(delivery_cost);
if !amount.is_zero() {
    T::Token::transfer(&sovereign_account, &who, amount, Preservation::Preserve)?;
}
``` [1](#0-0) 

`delivery_cost` is the intended fixed target the relayer should be paid — analogous to `debt` in the Revert Lend bug. The actually paid `amount` is `min(reducible_balance(sovereign_account), delivery_cost)` — analogous to `liquidatorCost = penaltyValue` being tied to the shrinking `fullValue` instead of the fixed `debt`. As the sovereign account's balance decreases (from repeated message delivery drains, since every `submit` call withdraws `delivery_cost` from it, or from any other outflow), the relayer's payout shrinks proportionally, all the way down to zero, while the fixed cost of actually relaying a message (calling a transaction, paying gas/fees, verifying proofs) does not change.

Critically, the message is still fully processed — verified, decoded, XCM dispatched, fees burned — **regardless of whether the relayer is compensated**:
```rust
if !amount.is_zero() { ... } // reward optional, no failure if amount == 0
...
let (xcm, fee) = Self::do_convert(envelope.message_id, message.clone())?;
Self::burn_fees(channel.para_id, fee)?;
let message_id = Self::send_xcm(xcm, channel.para_id)?;
``` [2](#0-1) 

This is confirmed by the pallet's own test suite, which explicitly documents that relaying continues to succeed with zero or partial reward once the sovereign account is drained:
```rust
#[test]
fn test_submit_no_funds_to_reward_relayers_just_ignore() {
    ...
    Balances::set_balance(&sovereign_account, 0);
    ...
    // Check submit successfully in case no funds available
    assert_ok!(InboundQueue::submit(origin.clone(), event.clone()));
}
``` [3](#0-2) 
```rust
#[test]
fn test_submit_no_funds_to_reward_relayers_and_ed_preserved() {
    ...
    Balances::set_balance(&sovereign_account, ExistentialDeposit::get() + 1);
    ...
    let amount = Balances::balance(&sovereign_account);
    assert_eq!(amount, ExistentialDeposit::get());
    ...
}
``` [4](#0-3) 

There is no top-up/backpressure mechanism in `submit` that halts message intake, refunds the shortfall later, or raises `delivery_cost` demanded from the message sender when the sovereign account is low — the shortfall is silently absorbed by the relayer, exactly as underwater liquidators silently absorbed the shortfall in the original bug.

## Impact Explanation
Because relayer compensation is capped to `reducible_balance(sovereign_account)` rather than always guaranteed to equal `delivery_cost`, rational relayers stop submitting Ethereum→Polkadot messages once a destination parachain's sovereign account balance is low or exhausted (a state that is realistically reachable simply through sustained normal traffic if the parachain does not proactively replenish the account, or through any burst of message volume that drains it faster than it is topped up). This is a direct instance of "public underpriced work that degrades block production or stalls bridge processing": the permissionless `submit` extrinsic remains callable and does real verification/XCM-dispatch work, but nobody is economically incentivized to call it, so the inbound message queue backs up on the source (Ethereum) side with no way for the runtime itself to detect or react to the payment shortfall.

## Likelihood Explanation
No privileged actor, governance action, or malicious node is required — this occurs under ordinary operation once the sovereign account's balance is depleted (through legitimate message-driven `delivery_cost` withdrawals or any other transfer out of that account). The condition is directly demonstrated by the pallet's own tests (`test_submit_no_funds_to_reward_relayers_just_ignore`, `test_submit_no_funds_to_reward_relayers_and_ed_preserved`), which treat zero/partial reward as an accepted, "successfully ignored" code path rather than a rejected one. This makes the disincentive condition trivially and repeatedly reachable by any unprivileged submitter/relayer.

## Recommendation
Decouple the guarantee of relayer payment from the momentary balance of a single sovereign account: e.g., accrue any shortfall (`delivery_cost - amount`) as a claimable debt against the parachain (similar to how `pallet-bridge-relayers`/`RewardLedger` tracks rewards for later claiming elsewhere in this codebase, see `bridges/primitives/relayers/src/lib.rs`), rather than transferring only `min(balance, delivery_cost)` and discarding the difference. Alternatively, gate message dispatch on the sovereign account holding sufficient funds to fully cover `delivery_cost`, so that underfunded channels visibly halt (a detectable, actionable failure) instead of silently producing unpaid relaying work.

## Proof of Concept
1. Deploy the inbound-queue pallet with a destination parachain sovereign account funded to just above `ExistentialDeposit`.
2. Call `submit` with a valid message/proof; observe (as in `test_submit_no_funds_to_reward_relayers_and_ed_preserved`) that the relayer receives `sovereign_account_balance - ED` instead of the full `delivery_cost`, and the sovereign account is drained to exactly `ED`. [5](#0-4) 
3. Submit a second valid message (different nonce) with the sovereign account now at `ED`; observe (as in `test_submit_no_funds_to_reward_relayers_just_ignore`) that `submit` still succeeds and fully processes/dispatches the message, but the relayer receives `0` reward. [3](#0-2) 
4. Because relaying is fully functional and unpaid, no automated mechanism in `submit` throttles intake or raises an alarm — a sustained backlog on the Ethereum side accumulates with no relayer willing to service it, mirroring the "disincentivized to act exactly when needed" invariant break from the seed report.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L269-281)
```rust
			// Reward relayer from the sovereign account of the destination parachain, only if funds
			// are available
			let sovereign_account = sibling_sovereign_account::<T>(channel.para_id);
			let delivery_cost = Self::calculate_delivery_cost(event.encode().len() as u32);
			let amount = T::Token::reducible_balance(
				&sovereign_account,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.min(delivery_cost);
			if !amount.is_zero() {
				T::Token::transfer(&sovereign_account, &who, amount, Preservation::Preserve)?;
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L283-308)
```rust
			// Decode payload into `VersionedMessage`
			let message = VersionedMessage::decode_all(&mut envelope.payload.as_ref())
				.map_err(|_| Error::<T>::InvalidPayload)?;

			// Decode message into XCM
			let (xcm, fee) = Self::do_convert(envelope.message_id, message.clone())?;

			tracing::info!(
				target: LOG_TARGET,
				?xcm,
				?fee,
				"💫 xcm decoded"
			);

			// Burning fees for teleport
			Self::burn_fees(channel.para_id, fee)?;

			// Attempt to send XCM to a dest parachain
			let message_id = Self::send_xcm(xcm, channel.para_id)?;

			Self::deposit_event(Event::MessageReceived {
				channel_id: envelope.channel_id,
				nonce: envelope.nonce,
				message_id,
				fee_burned: fee,
			});
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/test.rs (L148-169)
```rust
#[test]
fn test_submit_no_funds_to_reward_relayers_just_ignore() {
	new_tester().execute_with(|| {
		let relayer: AccountId = Keyring::Bob.into();
		let origin = RuntimeOrigin::signed(relayer);

		// Reset balance of sovereign_account to zero first
		let sovereign_account = sibling_sovereign_account::<Test>(ASSET_HUB_PARAID.into());
		Balances::set_balance(&sovereign_account, 0);

		// Submit message
		let event = EventProof {
			event_log: mock_event_log(),
			proof: Proof {
				receipt_proof: Default::default(),
				execution_proof: mock_execution_proof(),
			},
		};
		// Check submit successfully in case no funds available
		assert_ok!(InboundQueue::submit(origin.clone(), event.clone()));
	});
}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/test.rs (L206-244)
```rust
#[test]
fn test_submit_no_funds_to_reward_relayers_and_ed_preserved() {
	new_tester().execute_with(|| {
		let relayer: AccountId = Keyring::Bob.into();
		let origin = RuntimeOrigin::signed(relayer);

		// Reset balance of sovereign account to (ED+1) first
		let sovereign_account = sibling_sovereign_account::<Test>(ASSET_HUB_PARAID.into());
		Balances::set_balance(&sovereign_account, ExistentialDeposit::get() + 1);

		// Submit message successfully
		let event = EventProof {
			event_log: mock_event_log(),
			proof: Proof {
				receipt_proof: Default::default(),
				execution_proof: mock_execution_proof(),
			},
		};
		assert_ok!(InboundQueue::submit(origin.clone(), event.clone()));

		// Check balance of sovereign account to ED
		let amount = Balances::balance(&sovereign_account);
		assert_eq!(amount, ExistentialDeposit::get());

		// Submit another message with nonce set as 2
		let mut event_log = mock_event_log();
		event_log.data[31] = 2;
		let event = EventProof {
			event_log,
			proof: Proof {
				receipt_proof: Default::default(),
				execution_proof: mock_execution_proof(),
			},
		};
		assert_ok!(InboundQueue::submit(origin.clone(), event.clone()));
		// Check balance of sovereign account as ED does not change
		let amount = Balances::balance(&sovereign_account);
		assert_eq!(amount, ExistentialDeposit::get());
	});
```
