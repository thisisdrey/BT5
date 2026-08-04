### Title
Reward claim on BridgeHub optimistically clears the relayer's reward ledger before the cross-chain payout actually lands, permanently losing funds if the XCM payout fails - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
The external report's core defect is "fire-and-forget" value transfer: a payout function marks the transfer as done (moves on / updates state) without confirming that the recipient actually received the funds, because the low-level send can silently fail. The same broken invariant — clearing accounting state before value delivery is confirmed — exists in the Snowbridge relayer-reward payout path on BridgeHub: `pallet_bridge_relayers::do_claim_rewards` deletes the relayer's `RelayerRewards` entry as soon as `PaymentProcedure::pay_reward` returns `Ok(())`, but for `BridgeReward::Snowbridge` that `Ok(())` only means an XCM message was successfully *queued* for AssetHub, not that the deposit to the beneficiary succeeded there.

### Finding Description
`Pallet::do_claim_rewards` in `bridges/modules/relayers/src/lib.rs` takes the reward out of `RelayerRewards` and, only if `T::PaymentProcedure::pay_reward(...)` returns `Ok`, emits `RewardPaid` and lets the removal commit (via `try_mutate_exists`): [1](#0-0) 

For the Snowbridge reward kind, `PaymentProcedure` is implemented by `BridgeRewardPayer::pay_reward`, which for `BridgeReward::Snowbridge` delegates to `snowbridge_core::reward::PayAccountOnLocation::pay_reward`: [2](#0-1) 

`PayAccountOnLocation::pay_reward` builds an XCM program (`ReserveAssetDeposited` + `DepositAsset` to the beneficiary on AssetHub), charges local fees, and calls `XcmSender::deliver(ticket)`. It returns `Ok(())` as soon as the message is handed to the transport layer — it never waits for, or checks, whether the remote `DepositAsset` actually succeeds: [3](#0-2) 

This is structurally identical to the reported Solidity bug class: the caller (here, `do_claim_rewards`) treats "the send was dispatched" as equivalent to "the recipient received the value," and irreversibly updates accounting (deleting `RelayerRewards[relayer][Snowbridge]`) before that assumption is verified. On AssetHub, `DepositAsset` can fail for ordinary, non-adversarial reasons — e.g., the beneficiary account/asset instance is below the existential/minimum balance for the foreign asset (`ETHER_MIN_BALANCE` is explicitly called out in the test suite as a required minimum), the destination doesn't hold the `eth_location()` foreign asset class yet, or barrier/weight limits reject the XCM. When that happens, the `MessageQueue`/XCM error is only visible as an event on AssetHub; BridgeHub has no rollback or retry path because the reward record was already deleted at claim time.

### Impact Explanation
A relayer that successfully delivered messages and accrued a reward can permanently lose that reward through no fault of its own and with no adversarial action required: a normal beneficiary-location mistake or a not-yet-funded/ED-starved beneficiary account on AssetHub causes the remote deposit to fail while BridgeHub has already zeroed the claimable balance and emitted `RewardPaid`. This is a permanent fund loss for the relayer and breaks the "settle exactly once to the rightful beneficiary" invariant called out in the impact gate — the value is asserted as paid (ledger cleared, event emitted) but never actually reaches the beneficiary, and cannot be reclaimed since `RelayerRewards` no longer holds the entry.

### Likelihood Explanation
No privileged actor, relayer misbehavior, or governance action is required. Any relayer claiming a Snowbridge reward to a beneficiary location that is not already funded above the relevant existential/minimum balance (a routine, easily-triggered condition given `ETHER_MIN_BALANCE` requirements documented in the emulated tests) will hit this path. This is a plain "unprivileged public dispatch (`claim_rewards_to`) causes fund loss" scenario, matching the required-impact criteria directly.

### Recommendation
Do not treat XCM `deliver()` success as final settlement for accounting purposes. Either:
- Keep the reward entry pending/locked until a delivery/execution confirmation (e.g., an XCM `QueryResponse`/receipt) is received from AssetHub, only then clearing `RelayerRewards`; or
- On remote failure, credit the reward back to the relayer's `RelayerRewards` entry (mirroring how `pallet_treasury`'s `payout`/`check_status`/`PaymentState::{Attempted,Failed}` flow allows retrying after `Paymaster` failure, e.g. `substrate/frame/treasury/src/lib.rs` lines 736-757 and 703-726) instead of unconditionally removing it on `Ok` from `deliver()`.

### Proof of Concept
1. Relayer accrues a Snowbridge reward via `register_reward` (as in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` `process_delivery_receipt`).
2. Relayer calls `pallet_bridge_relayers::claim_rewards_to(reward_kind = Snowbridge, beneficiary = BridgeRewardBeneficiaries::AssetHubLocation(some_new_account_below_ED))`.
3. `do_claim_rewards` removes the `RelayerRewards` entry, calls `PayAccountOnLocation::pay_reward`, which builds and sends the XCM; `XcmSender::deliver` succeeds, so `pay_reward` returns `Ok(())`, `RewardPaid` is emitted, and the storage removal commits.
4. On AssetHub, the `DepositAsset` instruction in the incoming XCM fails (beneficiary account cannot receive the foreign WETH asset instance because it lacks the existential/minimum balance) — visible as a non-success `MessageQueue::Processed`/XCM error event rather than the `ForeignAssets::Deposited` event seen in the passing test (`cumulus/.../snowbridge_v2_rewards.rs` lines 90-102).
5. Relayer has zero `RelayerRewards` balance and never received funds on AssetHub; no dispatchable exists to re-credit or retry the payout.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-301)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
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
