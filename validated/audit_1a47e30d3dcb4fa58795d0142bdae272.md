Based on the investigation, I found a genuine local analog: **the Snowbridge relayer-reward payout procedure treats successful XCM dispatch as proof of final payment, exactly like the reported `transfer()` bug where the caller assumes success without confirming the recipient actually received funds.**

### Title
Snowbridge relayer reward is marked paid and removed from claimable state before the cross-chain mint is confirmed to succeed - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`PaymentProcedure::pay_reward` for Snowbridge rewards (`PayAccountOnLocation`) returns `Ok(())` as soon as the reward-minting XCM is *handed to the router* (`XcmSender::deliver`), not once the `DepositAsset` on AssetHub actually completes. `pallet-bridge-relayers`'s `claim_rewards_to` uses this `Ok(())` to irreversibly clear the relayer's reward entry on BridgeHub. This mirrors the reported bug class: a value-moving call whose "success" is only apparent/local, while the real transfer can still fail downstream (bad beneficiary, dust/ED issues, asset registration problems) — leaving the relayer's funds unrecoverable except through the unrelated `pallet_xcm::claim_assets` trapped-asset recovery path, which only works if the user happened to encode a claimer that matches their signing key.

### Finding Description
`PayAccountOnLocation::pay_reward` builds and sends an `UnpaidExecution` XCM program ending in `DepositAsset { assets: AllCounted(1), beneficiary }` to AssetHub: [1](#0-0) 

It calls `validate_send`, `XcmExecutor::charge_fees`, and `XcmSender::deliver`, then returns `Ok(())` unconditionally on successful delivery — with no confirmation that the remote `DepositAsset` instruction executes without error (e.g., if the asset isn't registered, the beneficiary balance drops below the asset's minimum balance, or the `ReserveAssetDeposited`/`DepositAsset` pairing traps the asset).

This procedure is wired into `BridgeRewardPayer::pay_reward`, invoked from `pallet-bridge-relayers`'s `claim_rewards_to` extrinsic: [2](#0-1) 

Because `pay_reward` reports success purely based on local XCM queuing, `claim_rewards_to` on BridgeHub finalizes the claim (clearing the relayer's `RelayerRewards` entry) even though the actual value transfer on AssetHub is not yet — and may never be — settled. The test suite itself demonstrates that assets *can* become trapped on AssetHub under adverse conditions (invalid payload, mismatched claimer), with recovery only possible if the user encoded a claimer whose derived account matches their own signing origin: [3](#0-2) 

Since the reward beneficiary in `PayAccountOnLocation` is chosen by the relayer at claim time (not necessarily matching a claimer used at message-processing time), a failed `DepositAsset` for the reward-mint XCM traps the asset with no guaranteed recovery path for the relayer, while BridgeHub has already marked the reward as paid.

### Impact Explanation
This breaks the required invariant that "bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer's registered reward can be permanently lost (funds trapped/locked on AssetHub, unreachable by the intended beneficiary) while BridgeHub's bookkeeping shows the reward as fully paid, with `Event::RewardPaid` emitted despite no confirmed value delivery. This is a fund-loss/fund-lock condition triggered purely by normal protocol conditions (asset registration state, existential deposit, or beneficiary account state on the destination chain), not by a malicious actor, satisfying the "permanent user-fund lock" / "duplicate settlement... only advance after settlement succeeds" impact categories.

### Likelihood Explanation
This path is reachable by any relayer calling the permissionless `claim_rewards_to` extrinsic with an `AssetHubLocation` beneficiary of their choosing under normal-but-unfavorable conditions (e.g., first-time / unfunded beneficiary account, or asset-registration edge cases on AssetHub) — no privileged actor, governance action, or malicious peer is required. The likelihood is data/state dependent (depends on AssetHub-side conditions being unfavorable at the moment of the XCM's arrival) rather than always exploitable, but the code path provides no synchronous confirmation or retry/reconciliation mechanism, so it is a structurally sound but unverified-analog finding; I could not fully trace the exact sequencing inside `claim_rewards_to`/`do_claim_rewards` in `bridges/modules/relayers/src/lib.rs` due to running out of investigation budget, so the precise point at which the `RelayerRewards` entry is cleared relative to `pay_reward`'s return should be double-checked by the assigned engineer.

### Recommendation
- Do not treat `XcmSender::deliver` success as final settlement for `PayAccountOnLocation::pay_reward`; either require a confirmed-execution callback/receipt from AssetHub before clearing the reward record on BridgeHub, or make the claim state reversible/retryable until settlement is confirmed (similar to the `PayoutAttempted`/`check_status` pattern already used in `substrate/frame/multi-asset-bounties`).
- Alternatively, adopt an async two-phase payout: mark the reward as `PayoutAttempted` on send, and only finalize/clear it after an explicit confirmation of `DepositAsset` success (e.g., via a receipt XCM or a `check_status`-style extrinsic), matching the design already used in `pallet-multi-asset-bounties`'s `BountyStatus::PayoutAttempted`/`RefundAttempted` state machine.
- Ensure a robust recovery path exists for relayers whose reward XCM traps assets, independent of whether they can craft a matching claimer.

### Proof of Concept
1. Relayer accumulates a Snowbridge reward via `BridgeRelayers::register_reward(relayer, BridgeReward::Snowbridge, reward_amount)`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(some_new_or_unregistered_account_location))`.
3. `BridgeRewardPayer::pay_reward` routes to `PayAccountOnLocation::pay_reward`, which builds the `DepositAsset` XCM and calls `XcmSender::deliver` — returning `Ok(())` purely on queuing.
4. On AssetHub, `DepositAsset` fails (e.g., beneficiary account cannot receive the asset due to ED/registration state), asset is trapped (`PolkadotXcm::AssetsTrapped`), as reproduced by the trapped-asset scenario in the test suite: [4](#0-3) 
5. On BridgeHub, `RewardPaid` is already emitted and the reward record is gone — the relayer has no recorded claimable balance left, and can only recover the trapped asset by satisfying `claim_assets` origin-matching requirements it did not necessarily set up when calling `claim_rewards_to`.

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-136)
```rust
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
						let account_location = Location::try_from(account_location)
							.map_err(|_| Self::Error::Other("`AssetHubLocation` beneficiary location version is not supported for `Snowbridge` rewards!"))?;
						snowbridge_core::reward::PayAccountOnLocation::<
							AccountId,
							u128,
							EthereumNetwork,
							AssetHubLocation,
							InboundQueueV2Location,
							XcmRouter,
							XcmExecutor<XcmConfig>,
							RuntimeCall
						>::pay_reward(
							relayer, (), reward, account_location
						)
					}
				}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L985-1021)
```rust
	// Message still processes successfully
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		assert_expected_events!(
			AssetHubWestend,
			vec![
				// Token was issued to beneficiary
				RuntimeEvent::ForeignAssets(pallet_assets::Event::Deposited { asset_id, who: owner, .. }) => {
					asset_id: *asset_id == weth_location(),
					owner: *owner == beneficiary_acc.into(),
				},
				// Leftover fees deposited to beneficiary
				RuntimeEvent::ForeignAssets(pallet_assets::Event::Deposited { asset_id, who: owner, .. }) => {
					asset_id: *asset_id == eth_location(),
					owner: *owner == beneficiary_acc.into(),
				},
			]
		);

		// Beneficiary received the token transfer value
		assert_eq!(
			ForeignAssets::balance(weth_location(), AccountId::from(beneficiary_acc)),
			token_transfer_value
		);

		let events = AssetHubWestend::events();
		// Check that no assets were trapped
		assert!(
			!events.iter().any(|event| matches!(
				event,
				RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsTrapped { .. })
			)),
			"Assets were trapped, should not happen."
		);
	});
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1158-1182)
```rust
/// When the user supplies their own claimer (anchored on the local network) in the
/// inbound message and the XCM payload then traps assets on AH, the user can sign
/// `pallet_xcm::claim_assets` to recover those assets — because the claimer location
/// they encoded matches the location produced by AH's `SignedToAccountId32` from
/// their signed origin.
#[test]
fn user_supplied_claimer_can_claim_trapped_assets() {
	use asset_hub_westend_runtime::xcm_config::RelayNetwork as AssetHubRelayNetwork;

	let relayer_account = BridgeHubWestendSender::get();
	let relayer_reward = 1_500_000_000_000u128;
	let local_network = AssetHubRelayNetwork::get().expect("RelayNetwork is Some");

	// User account that owns the claimer; will also sign claim_assets on AH.
	let user_account = AssetHubWestendReceiver::get();
	// Fund so the deposit-after-claim has the ED.
	AssetHubWestend::fund_accounts(vec![(user_account.clone(), INITIAL_FUND)]);

	// Claimer the user encodes in the inbound message. Anchored on the local network
	// so it matches what `SignedToAccountId32` will produce on AH for their origin.
	let user_claimer = Location::new(
		0,
		[AccountId32Junction { network: Some(local_network), id: user_account.clone().into() }],
	);
	let user_claimer_bytes = user_claimer.encode();
```
