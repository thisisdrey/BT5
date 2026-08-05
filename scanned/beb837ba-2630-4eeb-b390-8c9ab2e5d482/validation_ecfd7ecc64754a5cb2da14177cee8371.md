### Title
`claim_rewards_to()` commits reward removal on BridgeHub before the cross-chain payout to AssetHub is guaranteed to succeed, permanently destroying relayer rewards below the destination minimum balance - (File: `bridges/modules/relayers/src/lib.rs`, `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
This is a direct structural analog of the Votium `applyRewards()` bug: a payout path silently discards funds when the transferred amount does not meet a downstream minimum-balance requirement, because the "commit" of the payout state happens before the actual value transfer is confirmed to succeed.

### Finding Description
`Pallet::do_claim_rewards` in `bridges/modules/relayers/src/lib.rs` (lines 263-302) uses `RelayerRewards::<T, I>::try_mutate_exists` to take the accumulated reward out of storage and then calls `T::PaymentProcedure::pay_reward(...)`. If `pay_reward` returns `Ok(())`, the storage mutation (removal of the reward entry) is committed and `Event::RewardPaid` is emitted [1](#0-0) .

For the Snowbridge reward configuration, `T::PaymentProcedure` is `PayAccountOnLocation` (`bridges/snowbridge/primitives/core/src/reward.rs`, lines 127-151). Its `pay_reward` only validates and dispatches an `UnpaidExecution` XCM to AssetHub containing `ReserveAssetDeposited` + `DepositAsset` — it returns `Ok(())` as soon as `validate_send`, `charge_fees`, and `deliver` succeed on **BridgeHub**: [2](#0-1) 

Crucially, `pay_reward` never checks whether `reward` meets the minimum balance required to actually mint/deposit the foreign WETH asset on AssetHub (`ETHER_MIN_BALANCE`, referenced in the emulated test suite: [3](#0-2) ). It fires the XCM and returns success on BridgeHub regardless of what happens when that message is actually executed by the XCM executor on AssetHub. If the reward amount is below the asset's minimum balance on AssetHub, `pallet_assets`/`ForeignAssets`' `DepositAsset` will fail on execution (the deposit is rejected because it falls under the asset's existential/minimum balance), causing the assets to be trapped or burned on AssetHub.

By the time that failure occurs, however:
- The relayer's `RelayerRewards` entry has already been deleted on BridgeHub (`try_mutate_exists` committed because the closure returned `Ok`).
- `Event::RewardPaid` has already been emitted, telling downstream observers the reward was paid.

This mirrors exactly the `SafEth.stake()` bug: a downstream minimum-amount requirement (`minAmount`/`ETHER_MIN_BALANCE`) is not accounted for before the "already succeeded" state is committed, so smaller-than-threshold amounts are silently lost rather than causing the whole claim to revert or be retried.

### Impact Explanation
This falls under "permanent user-fund lock" and "message/payout state must only advance after settlement succeed atomically," both explicitly in scope. A relayer whose accumulated Snowbridge reward is smaller than `ETHER_MIN_BALANCE` (which is entirely plausible for a single small message, or for accumulated dust after multiple partial claims) permanently loses that reward: it is deleted from `RelayerRewards` on BridgeHub, the `RewardPaid` event fires, but the value never lands in the relayer's account on AssetHub because the deposit is rejected downstream. There is no retry mechanism, and the relayer has no way to reclaim the burnt/trapped value since the pallet's local record has already been removed.

### Likelihood Explanation
Any relayer can trigger `claim_rewards_to()` (a public, unprivileged, signed extrinsic) whenever they have any reward balance registered, no matter how small. There is no check anywhere in `RelayerRewards`, `do_claim_rewards`, or `PayAccountOnLocation::pay_reward` gating the claim on the AssetHub-side minimum balance. This is a straightforward, unprivileged, always-reachable path — not requiring any malicious peer, relayer, validator, or governance action.

### Recommendation
- Enforce a minimum claimable reward equal to (or greater than) the destination asset's minimum balance requirement before allowing `claim_rewards_to` to proceed (fail early with a clear error rather than silently losing funds), analogous to the Votium fix of wrapping the deposit call and checking thresholds beforehand.
- Alternatively, make `PaymentProcedure::pay_reward` genuinely atomic with respect to the destination-chain deposit — e.g., use an XCM query/response pattern to detect execution failure on AssetHub and only remove the `RelayerRewards` entry (and emit `RewardPaid`) once execution success is confirmed, or restore the reward entry on failure receipt.
- Add an explicit `ensure!(reward_balance >= T::MinimumClaimableReward::get(), Error::<T, I>::RewardBelowMinimum)` guard in `do_claim_rewards` before calling `pay_reward`.

### Proof of Concept
1. A relayer's `RelayerRewards` entry accumulates a small reward, e.g. `reward_balance = ETHER_MIN_BALANCE - 1`, via `register_reward` after relaying an inbound/outbound Snowbridge message (see `process_message`/`process_delivery_receipt` reward registration: [4](#0-3) ).
2. The relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(...))`.
3. `do_claim_rewards` takes the reward from storage, calls `PayAccountOnLocation::pay_reward`, which validates/delivers the XCM and returns `Ok(())` on BridgeHub — the storage entry is deleted and `RewardPaid` is emitted.
4. On AssetHub, the `DepositAsset` instruction executes with an amount below the foreign asset's minimum balance; `pallet_assets` rejects the deposit, and the asset is trapped/burnt.
5. The relayer has no reward left in `RelayerRewards` (deleted in step 3) and never receives funds on AssetHub — permanent loss, with no error surfaced on BridgeHub since `pay_reward` already returned `Ok`.

This scenario is not exercised by the existing test `claim_snowbridge_rewards_to_local_account_fails` (which only tests a `LocalAccount` beneficiary variant that is rejected before any XCM is even built) nor by `claim_rewards_works` (which deliberately uses `reward_amount = ETHER_MIN_BALANCE * 2` to stay above the threshold, tacitly acknowledging the risk): [5](#0-4) .

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L268-299)
```rust
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
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L136-151)
```rust
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L39-49)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <BridgeHubWestend as Chain>::RuntimeOrigin;
		let reward_amount = ETHER_MIN_BALANCE * 2; // Reward should be more than Ether min balance

		type BridgeRelayers = <BridgeHubWestend as BridgeHubWestendPallet>::BridgeRelayers;
		BridgeRelayers::register_reward(
			(&relayer_account.clone()).into(),
			BridgeReward::Snowbridge,
			reward_amount,
		);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L119-130)
```rust
	BridgeHubWestend::execute_with(|| {
		type Runtime = <BridgeHubWestend as Chain>::Runtime;
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <BridgeHubWestend as Chain>::RuntimeOrigin;
		let reward_amount = ETHER_MIN_BALANCE * 2; // Reward should be more than Ether min balance

		type BridgeRelayers = <BridgeHubWestend as BridgeHubWestendPallet>::BridgeRelayers;
		BridgeRelayers::register_reward(
			&relayer_account.clone(),
			BridgeReward::Snowbridge,
			reward_amount,
		);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L464-473)
```rust
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
