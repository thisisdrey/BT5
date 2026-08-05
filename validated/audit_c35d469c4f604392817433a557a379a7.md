Audit Report

## Title
Relayer reward for delivering Ethereum→Polkadot messages is capped by the sovereign account's shrinking balance instead of the fixed delivery cost, disincentivizing relaying and stalling bridge processing - (File: `bridges/snowbridge/pallets/inbound-queue/src/lib.rs`)

## Summary
`Pallet::<T>::submit` computes the fixed delivery cost via `Self::calculate_delivery_cost(...)` but pays the relayer only `min(reducible_balance(sovereign_account), delivery_cost)`, silently truncating the reward whenever the destination parachain's sovereign account balance is insufficient. Message verification, XCM conversion, fee burning, and dispatch proceed unconditionally regardless of whether the relayer was paid, so unpaid or underpaid relaying is a fully successful code path rather than a rejected one.

## Finding Description
In `submit()`, after nonce verification, the reward logic is: [1](#0-0) 

`delivery_cost` (from `calculate_delivery_cost`, which sums `WeightToFee`, `LengthToFee`, and `PricingParameters::rewards.local`) is the intended fixed target the relayer should receive for the real-world cost of relaying. [2](#0-1)  But the actual `amount` transferred is capped to `reducible_balance(sovereign_account, Preservation::Preserve, Fortitude::Polite)`, and if that balance is low (e.g. drained by repeated `delivery_cost` withdrawals from prior `submit` calls), the payout shrinks toward zero while the guarded `if !amount.is_zero()` simply skips the transfer instead of failing the extrinsic. [3](#0-2) 

Critically, message processing continues unconditionally afterward — decoding, XCM conversion, fee burning, and `send_xcm` all execute regardless of the reward outcome: [4](#0-3) 

This is explicitly exercised by the pallet's own tests, which treat zero/partial reward as a successful, accepted path: [5](#0-4) 

There is no mechanism in `submit` to halt intake, accrue the shortfall as claimable debt, or otherwise guarantee the relayer receives the full `delivery_cost` when the sovereign account is underfunded — the shortfall is simply absorbed by the relayer.

Notably, this exact design flaw appears to have been addressed in a newer version of the pallet: the v2 inbound queue (`snowbridge-pallet-inbound-queue-v2`) registers relayer rewards through `pallet_bridge_relayers::Event::RewardRegistered` (a claimable-ledger model) rather than performing an immediate capped transfer from a sovereign account, as shown in `send_token_v2` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs`. [6](#0-5)  A separate PR (`prdoc/stable2506/pr_8271.prdoc`) also introduces reward tip top-ups specifically to address cases where "the relayer reward is too low and not profitable to process." [7](#0-6)  However, the legacy `inbound-queue` (v1) pallet analyzed here, still present and shipped in this repository (e.g. wired as `EthereumInboundQueue` in BridgeHub Westend integration tests), retains the flawed `min(balance, delivery_cost)` behavior with no such safeguard.

## Impact Explanation
Because relayer compensation for the permissionless `submit` extrinsic is capped to the sovereign account's momentary `reducible_balance` rather than guaranteed to equal the fixed `delivery_cost`, rational relayers lose the economic incentive to submit Ethereum→Polkadot messages once a destination parachain's sovereign account balance is low or exhausted — a state reachable purely through sustained normal traffic (each `submit` call withdraws `delivery_cost` from that same account) without proactive replenishment. This matches the accepted impact category of "public underpriced work that degrades block production or stalls bridge processing": the extrinsic remains callable and still performs full verification/XCM-dispatch work, but nobody is compensated for calling it, so the inbound message queue can back up on the Ethereum side with no on-chain detection or throttling mechanism.

## Likelihood Explanation
No privileged actor or malicious behavior is required. This occurs under ordinary operation whenever the sovereign account's balance is depleted through the pallet's own normal `delivery_cost` withdrawals or any other transfer out of it. The pallet's own test suite (`test_submit_no_funds_to_reward_relayers_just_ignore`, `test_submit_no_funds_to_reward_relayers_and_ed_preserved`) demonstrates and accepts zero/partial-reward submission as a non-failing code path, confirming the condition is trivially and repeatedly reachable by any unprivileged submitter/relayer.

## Recommendation
Decouple the guarantee of relayer payment from the momentary sovereign-account balance: accrue any shortfall (`delivery_cost - amount`) as claimable debt (similar to the `pallet-bridge-relayers` reward-ledger model already used in the v2 inbound queue), or gate message dispatch on the sovereign account holding sufficient funds to cover `delivery_cost` in full so underfunded channels visibly halt instead of silently producing unpaid relaying work.

## Proof of Concept
1. Fund a destination parachain's sovereign account to just above `ExistentialDeposit`.
2. Call `submit` with a valid message/proof; the relayer receives only `sovereign_account_balance - ED` instead of the full `delivery_cost`, per `test_submit_no_funds_to_reward_relayers_and_ed_preserved`. [8](#0-7) 
3. Submit a second valid message (incremented nonce) with the sovereign account now at `ED`; `submit` still succeeds and fully dispatches the message, but the relayer receives `0` reward, per `test_submit_no_funds_to_reward_relayers_just_ignore`. [9](#0-8) 
4. No mechanism in `submit` throttles intake or signals the shortfall, so a sustained Ethereum-side backlog can accumulate with no relayer incentivized to service it.

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

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L343-349)
```rust
		pub fn calculate_delivery_cost(length: u32) -> BalanceOf<T> {
			let weight_fee = T::WeightToFee::weight_to_fee(&T::WeightInfo::submit());
			let len_fee = T::LengthToFee::weight_to_fee(&Weight::from_parts(length as u64, 0));
			weight_fee
				.saturating_add(len_fee)
				.saturating_add(T::PricingParameters::get().rewards.local)
		}
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L200-217)
```rust
		EthereumInboundQueueV2::process_message(relayer_account.clone(), message.clone()).unwrap();

		let topic_id = blake2_256(&("SnowbridgeInboundQueueV2", message.nonce).encode());
		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::XcmpQueue(cumulus_pallet_xcmp_queue::Event::XcmpMessageSent { .. }) => {},
				// Check that the relayer reward was registered.
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == relayer_reward,
				},
				RuntimeEvent::EthereumInboundQueueV2(snowbridge_pallet_inbound_queue_v2::Event::MessageReceived { message_id, .. }) => {
					message_id: *message_id == topic_id,
				},
			]
		);
```

**File:** prdoc/stable2506/pr_8271.prdoc (L1-6)
```text
title: Snowbridge - Message reward topups
doc:
- audience: Runtime Dev
  description: |-
     This PR enables the ability to add a tip to an Inbound or Outbound message, in case the relayer reward is too low
     and not profitable to process. The tip is added to the relayer reward when processing a message.
```
