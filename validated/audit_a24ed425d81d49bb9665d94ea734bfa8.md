## Analysis

The CoreDAO bug's core invariant break is: **an accounting/ledger value is decremented (treated as "paid") based on a local send() call succeeding, without confirming the recipient actually received the funds.** The strongest local analog is in the Snowbridge relayer-reward claim flow.

### Title
Relayer reward ledger is irrevocably cleared on mere XCM dispatch, not on confirmed delivery/deposit, permanently losing rewards on remote execution failure - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`pallet_bridge_relayers::do_claim_rewards` removes a relayer's `RelayerRewards` entry and emits `RewardPaid` as soon as `PaymentProcedure::pay_reward` returns `Ok`. For Snowbridge rewards, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which returns `Ok(())` the moment the reward-mint XCM is *validated, fee-charged, and handed to the XCM router* on BridgeHub — it never confirms that the remote `DepositAsset` on AssetHub actually credited the beneficiary.

### Finding Description
`do_claim_rewards` takes the reward value out of storage and commits that removal based solely on the `Result` of `pay_reward`: [1](#0-0) 

`PayAccountOnLocation::pay_reward` (used for `BridgeReward::Snowbridge` via `BridgeRewardPayer`) treats "local delivery succeeded" as "payment succeeded": [2](#0-1) 

It builds an XCM ending in `DepositAsset { assets: AllCounted(1).into(), beneficiary }` and returns `Ok(())` right after `XcmSender::deliver(ticket)` succeeds — i.e., after the message is merely queued for XCMP delivery to AssetHub. The actual mint/deposit into the beneficiary account happens later, asynchronously, on AssetHub, and can fail (e.g., `DepositAsset` traps assets when the beneficiary can't receive them, as demonstrated for the same reward-style XCM shape by the inbound-queue trap test): [3](#0-2) 

`BridgeRewardPayer::pay_reward` routes `BridgeReward::Snowbridge` exclusively through this XCM-fire-and-forget path: [4](#0-3) 

Because `do_claim_rewards` uses `try_mutate_exists` and only rolls back on `Err`, and `pay_reward` returns `Ok` before remote settlement is known, the `RelayerRewards` entry (the exact corrupted value) is deleted and `RewardPaid` is emitted even when the beneficiary never receives the WETH. This is structurally identical to the CoreDAO `remain` bug: the accounting state is advanced on the strength of a *local send attempt* rather than a *confirmed receipt*.

Contrast this with `pallet-treasury`'s payout flow, which explicitly models `PaymentState::Attempted`/`Failed`/`Succeeded` and only clears the spend after `check_status` confirms `PaymentStatus::Success` via the paymaster: [5](#0-4) 
No equivalent pending/attempted/verify step exists for `pallet_bridge_relayers::claim_rewards_to` when the `PaymentProcedure` is asynchronous (XCM-based), unlike the synchronous `PayRewardFromAccount` case used for `RococoWestend` rewards.

### Impact Explanation
A relayer's earned reward can be silently and permanently destroyed with no retry path: the ledger entry is deleted, `RewardPaid` fires, but the beneficiary account on AssetHub never receives the WETH (assets are trapped or the deposit instruction fails for any of the usual XCM-execution reasons — insufficient existential deposit, asset not registered, weight/exceptions, etc.). This is unbacked value loss in a bridge reward-payout path with no attacker action required — it can happen from ordinary operational conditions (e.g., claiming to an AssetHub account/location that cannot yet hold the asset). This aligns with the "duplicate settlement or payout" / "permanent user-fund... lock" impact class for Snowbridge reward flows.

### Likelihood Explanation
This triggers under normal, permissionless usage of `claim_rewards_to` — no privileged actor, relayer collusion, or malicious peer is needed. Any relayer whose chosen beneficiary `Location` cannot successfully receive `DepositAsset` on AssetHub (misconfigured account, non-existent target below ED, wrong asset registration state, momentary AH congestion causing an XCM execution error) loses the reward permanently the moment `deliver()` succeeds locally.

### Recommendation
Do not remove the `RelayerRewards` entry (or mark the reward as paid) until remote execution success is confirmed. Adopt the same `PaymentState`-with-retry pattern used by `pallet-treasury`/`pallet-multi-asset-bounties`: record the reward as `Attempted` with a query/message id, and only clear/finalize it once a delivery/execution acknowledgment (e.g., an XCM query response, or a receipt analogous to `process_delivery_receipt`) confirms the deposit succeeded on AssetHub; otherwise revert to a `Failed`/`Pending` state that can be retried by the relayer.

### Proof of Concept
1. Register a Snowbridge reward for a relayer via `BridgeRelayers::register_reward(relayer, BridgeReward::Snowbridge, reward_amount)`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))` where `loc` resolves to an account/location on AssetHub that cannot accept the `DepositAsset` (e.g., account with balance below `ExistentialDeposit`, or asset not yet registered in that specific edge case).
3. On BridgeHub: `PayAccountOnLocation::pay_reward` validates the XCM, charges local fees, and `XcmSender::deliver` succeeds ⇒ `pay_reward` returns `Ok(())`.
4. `do_claim_rewards`'s `try_mutate_exists` commits: `RelayerRewards` entry removed, `RewardPaid` event emitted.
5. On AssetHub: the XCM executes; `DepositAsset` fails to credit the beneficiary and assets are trapped (`AssetsTrapped` event, per the existing `invalid_xcm_traps_funds_on_ah` test pattern) or otherwise lost.
6. Net result: relayer's reward ledger shows `RewardPaid`/is empty, but the relayer never received the reward and has no on-chain path to retry, mirroring the CoreDAO `remain` under-accounting after a failed transfer.

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L917-925)
```rust
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		// Assets are trapped
		assert_expected_events!(
			AssetHubWestend,
			vec![RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsTrapped { .. }) => {},]
		);
	});
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-137)
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
			}
```

**File:** substrate/frame/treasury/src/lib.rs (L795-813)
```rust
			let payment_id = match spend.status {
				State::Attempted { id } => id,
				_ => return Err(Error::<T, I>::NotAttempted.into()),
			};

			match T::Paymaster::check_payment(payment_id) {
				Status::Failure => {
					spend.status = PaymentState::Failed;
					Spends::<T, I>::insert(index, spend);
					Self::deposit_event(Event::<T, I>::PaymentFailed { index, payment_id });
				},
				Status::Success | Status::Unknown => {
					Spends::<T, I>::remove(index);
					Self::deposit_event(Event::<T, I>::SpendProcessed { index });
					return Ok(Pays::No.into());
				},
				Status::InProgress => return Err(Error::<T, I>::Inconclusive.into()),
			}
			return Ok(Pays::Yes.into());
```
