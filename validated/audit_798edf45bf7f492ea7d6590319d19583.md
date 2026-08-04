### Title
Relayer reward marked as paid on BridgeHub before AssetHub XCM execution succeeds, risking permanent reward loss — ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`PayAccountOnLocation::pay_reward` treats successful XCM *delivery* (message accepted into the outbound queue) as proof that the relayer reward was actually credited. It never verifies that the `DepositAsset` instruction executed successfully on AssetHub. Because `claim_rewards_to` on BridgeHub commits the "reward paid/consumed" storage change atomically with this `Ok(())` return, a later on-chain failure of the XCM program on AssetHub (destination side) cannot roll back the source-side settlement — the relayer's reward is irrecoverably marked as paid while the actual asset may never reach the intended beneficiary. This is the same broken invariant as the external report: an amount is computed and "sent" without confirming it was actually received, and the calling logic proceeds/commits regardless.

### Finding Description
`pallet-bridge-relayers::claim_rewards_to` (used by `BridgeReward::Snowbridge`) calls `T::PaymentProcedure::pay_reward(...)`, which for the Snowbridge reward kind resolves to `PayAccountOnLocation::pay_reward`: [1](#0-0) 

The function builds an XCM (`ReserveAssetDeposited` + `DepositAsset { beneficiary }`), validates it, charges delivery fees, and calls `XcmSender::deliver(ticket)`. It returns `Ok(())` as soon as `deliver` succeeds — i.e. as soon as the message is *accepted for delivery* to AssetHub. It does **not** wait for, or verify, that the `DepositAsset` instruction actually executed and credited the beneficiary on AssetHub.

Because FRAME dispatchables only roll back storage on `Err`, and `pay_reward` returns `Ok(())` at the point of enqueueing (not execution), the caller in `bridges/modules/relayers/src/lib.rs` (`claim_rewards_to`) commits the removal/consumption of the registered reward for that relayer as final. If the destination-side XCM later fails to execute `DepositAsset` (e.g. beneficiary account cannot be created because the reward net of AssetHub execution fees falls below existential deposit, or the beneficiary `Location` cannot be resolved to a valid account by AssetHub's converters), the assets are trapped via `PolkadotXcm::AssetsTrapped` on AssetHub rather than credited to the beneficiary — a scenario the codebase's own tests show is a real occurrence for this bridge's async message-processing flow: [2](#0-1) 

Recovery of trapped assets requires a manual `pallet_xcm::claim_assets` call whose success depends on the trap `origin` matching the claimer's `SignedToAccountId32` location exactly — the relayer has no first-class recovery path in this reward-payment flow, and no event/marker links the trap back to their consumed reward. Meanwhile, `claim_rewards_to` on BridgeHub already deposited a `RewardPaid` event and mutated/removed the pending reward, so the relayer cannot re-claim through the pallet: [3](#0-2) 

This matches the external report's core defect precisely: the pallet "calculates" the reward outcome (treats `deliver()` success as "amount transferred") without checking that the actual destination-side transfer/credit succeeded, and proceeds to finalize local state regardless.

### Impact Explanation
This is a permanent-fund-lock / duplicate-non-settlement class issue on live bridge infrastructure: a relayer's earned reward can be irrevocably consumed on BridgeHub while the corresponding asset never reaches (or reaches the wrong, unrecoverable) location on AssetHub. This falls squarely in "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" impact categories called out in the gate, since settlement state advances on the source chain without atomic confirmation of destination-side execution success.

### Likelihood Explanation
Any relayer who calls the permissionless `claim_rewards_to` extrinsic with an `AssetHubLocation` beneficiary that fails to execute cleanly on AssetHub (e.g., insufficient net amount after AH-side execution fees to satisfy ED, or a beneficiary derivation edge case) triggers this path without needing any privileged actor, malicious peer, or relayer collusion — the relayer is simply an ordinary unprivileged caller of their own reward-claim extrinsic. No governance, admin, or validator misbehavior is required.

### Recommendation
Do not treat XCM `deliver()` success as final settlement proof for `pay_reward`. Options:
- Use a receipt/acknowledgement mechanism (similar to the existing outbound-queue `process_delivery_receipt` pattern already used for bridge rewards) so the BridgeHub-side reward is only finally cleared after AssetHub confirms successful execution/deposit.
- Alternatively, keep the pending reward in a "in-flight" state (not fully removed) until a confirmation round-trip, and provide an automatic or well-defined reclaim path if the destination XCM traps the assets, tying the trap origin deterministically back to the claiming relayer so `claim_assets` is always available to them.

### Proof of Concept
1. Relayer accrues a Snowbridge reward via `register_reward` (source: message delivery receipt processing).
2. Relayer calls `claim_rewards_to(BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(beneficiary))` where `beneficiary` resolves to an AssetHub location such that, after AssetHub's local delivery/execution fees are deducted from the reward-denominated asset, the resulting `DepositAsset` either creates an account below existential deposit or otherwise fails to execute (see `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs:1023-1121` for a demonstrated case where an XCM program fails and traps assets on AssetHub under this bridge's flow).
3. `PayAccountOnLocation::pay_reward` returns `Ok(())` once `XcmSender::deliver` succeeds (`bridges/snowbridge/primitives/core/src/reward.rs:145-150`), so `claim_rewards_to` commits `RewardPaid` and clears the relayer's stored reward on BridgeHub.
4. On AssetHub, the XCM's `DepositAsset` fails; the reserve-deposited asset is trapped (`AssetsTrapped`) instead of credited to the relayer's beneficiary.
5. The relayer's reward is now unrecoverable through the reward pallet (already cleared/paid-event emitted), and recovery via `pallet_xcm::claim_assets` is not guaranteed to be available to them since the trap origin is derived from the XCM program's `UniversalOrigin`/`DescendOrigin` context, not the relayer's controlled account.

### Citations

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1023-1032)
```rust
/// When an inbound message arrives without a (valid) claimer, the converter falls back
/// to the bridge owner sovereign account, anchored on the local network. This test
/// drives the full claim flow: invalid XCM payload causes the holding register to be
/// trapped against that fallback location on Asset Hub, and the bridge owner signed
/// origin then claims the trapped assets via `pallet_xcm::claim_assets`.
///
/// Before the fix, the fallback claimer used `network: None`, so the trap origin did
/// not match the location produced by Asset Hub's `SignedToAccountId32` converter
/// (which always tags the local network), and the claim would fail with `UnknownClaim`.
#[test]
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L69-88)
```rust
		let result = BridgeRelayers::claim_rewards_to(
			RuntimeOrigin::signed(relayer_account.clone()),
			BridgeReward::Snowbridge,
			reward_beneficiary.clone(),
		);
		assert_ok!(result);

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				// Check that the pay reward event was emitted on BH
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardPaid { relayer, reward_kind, reward_balance, beneficiary }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == reward_amount,
					beneficiary: *beneficiary == reward_beneficiary,
				},
			]
		);
	});
```
