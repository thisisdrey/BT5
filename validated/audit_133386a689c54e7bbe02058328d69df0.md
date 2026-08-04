Analog found. The core broken invariant in the ReferralFeePoolV0 bug is: **claim state is finalized (or fails to be finalized correctly) independently of whether the underlying value transfer actually reaches the beneficiary**. In this repo the equivalent broken invariant appears not as a double-claim of the *same* reward, but as an **irreversible clearing of the relayer's claimable reward balance before the cross-chain settlement that is supposed to fund the beneficiary is confirmed** — i.e. payout state advances before execution/settlement succeeds, violating the required pivot that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Title
Snowbridge relayer reward claim clears local reward ledger on XCM *send* success, not on destination-chain settlement, causing permanent, unrecoverable loss of claimed rewards - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`pallet-bridge-relayers::claim_rewards_to` on BridgeHub finalizes ("takes") the relayer's `RelayerRewards` storage entry and treats the reward as paid as soon as `PaymentProcedure::pay_reward` returns `Ok(())`. For the Snowbridge reward kind, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which returns `Ok(())` immediately after the XCM instructing AssetHub to `ReserveAssetDeposited` + `DepositAsset` to the beneficiary has been **validated and handed to the transport layer for delivery** — not after the deposit actually lands on AssetHub. Because the storage mutation on BridgeHub is committed atomically with this `Ok(())`, the relayer's claim is irrevocably cleared even though the actual crediting on AssetHub is a separate, asynchronous, best-effort XCM execution that can fail (e.g. `DepositAsset` failing due to a bad/expired `VersionedLocation`, asset filtering, below-ED beneficiary, or execution weight issues), trapping the assets on AssetHub with no link back to the relayer's original claim.

### Finding Description
- `do_claim_rewards` in `bridges/modules/relayers/src/lib.rs` uses `RelayerRewards::<T, I>::try_mutate_exists` and does: [1](#0-0) 
  `maybe_reward.take()` removes the balance from storage, then calls `T::PaymentProcedure::pay_reward(...)`. If `pay_reward` returns `Err`, the whole closure returns `Err` and `try_mutate_exists` correctly rolls back the storage removal — this part is safe for *synchronous* payment procedures (like `PayRewardFromAccount`, a same-chain `Currency::transfer`).
- For the Snowbridge `BridgeReward::Snowbridge` variant, the configured procedure is `PayAccountOnLocation`, wired in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs`: [2](#0-1) 
- `PayAccountOnLocation::pay_reward` builds an XCM that performs `ReserveAssetDeposited` + `DepositAsset` on AssetHub and returns success purely based on local validate/charge-fees/deliver steps: [3](#0-2) 
  `validate_send`, `charge_fees`, and `XcmSender::deliver` only confirm that the message was accepted into the outbound XCMP/UMP transport — they say nothing about whether `DepositAsset` will succeed when the message is actually executed on AssetHub in a later block.
- Because `do_claim_rewards`'s `try_mutate_exists` commits the removal of `RelayerRewards` the moment `pay_reward` returns `Ok(())`, and `Event::RewardPaid` is emitted at that point too, the protocol has now told the relayer "your reward is paid" and erased the only record that a reward was owed — before the actual settlement (execution of `DepositAsset` on AssetHub) has happened.
- If the destination-side execution then fails (mis-versioned `VersionedLocation`, asset-filter rejection, beneficiary account non-existent/below ED for the reward asset, or any other execution error causing `AssetsTrapped`), the relayer has no reward record left on BridgeHub to re-claim, and the trapped assets are not straightforwardly claimable back to the relayer (the trap is registered under the BridgeHub sovereign/XCM origin context, not the relayer's account).
- Existing guards do not stop this: `try_mutate_exists` only protects against *local* failures signaled synchronously by `pay_reward`; it has no visibility into asynchronous XCM execution outcomes on the remote chain, and the pallet does not implement any receipt/callback/query-based confirmation (unlike `PayOverXcm`, which uses `QueryId` + `check_payment` for exactly this reason).

### Impact Explanation
This directly maps to "permanent user-fund or bridge-state lock" and violates the required pivot that payout state must only advance after execution and settlement succeed atomically. A relayer's earned reward can be permanently and unrecoverably erased from chain state while the actual token deposit on the destination chain silently fails, resulting in real fund loss for the relayer with no avenue to reclaim through the pallet's own claim flow.

### Likelihood Explanation
Likelihood is moderate: this doesn't require a malicious actor at all — it can be triggered by mundane conditions such as beneficiary account existential-deposit issues, asset-registration mismatches, or `VersionedLocation` version mismatches (the repo's own test `claim_snowbridge_rewards_to_local_account_fails` shows how easily a beneficiary variant can cause failures downstream of the initial send). Any relayer supplying a beneficiary location that AssetHub cannot successfully deposit into after the message is already accepted for delivery will lose the reward.

### Recommendation
Do not clear/finalize `RelayerRewards` (and do not emit `RewardPaid`) synchronously with the local `pay_reward` success for asynchronous, cross-chain payment procedures. Either:
1. Use a receipt/query-based confirmation pattern (similar to `PayOverXcm`'s `QueryId`/`check_payment`) so the local claim state is only cleared once destination-chain execution success is confirmed, or
2. Keep the reward pending in a "claim in flight" state and only fully remove it upon confirmation, with a path to restore/reclaim it if the remote execution is later observed to have failed (e.g., via `AssetsTrapped` correlation or a settlement callback).

### Proof of Concept
1. Register a Snowbridge reward for `relayer` via `register_reward`.
2. Call `claim_rewards_to` with a `BridgeRewardBeneficiaries::AssetHubLocation(...)` that is valid enough to pass `validate_send`/`charge_fees`/`deliver` on BridgeHub (e.g. a `VersionedLocation` that decodes and routes fine) but whose `DepositAsset` execution fails on AssetHub (e.g. beneficiary account that cannot hold the foreign asset without ED, or asset filter mismatch) — this mirrors the setup used in the passing test `claim_rewards_works` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs`, but with the AssetHub-side deposit made to fail instead of succeed.
3. Observe on BridgeHub: `Event::RewardPaid` is emitted and `RelayerRewards::get(relayer, BridgeReward::Snowbridge)` returns `None` (reward cleared).
4. Observe on AssetHub: no `pallet_assets::Event::Deposited` for the beneficiary; instead `pallet_xcm::Event::AssetsTrapped` or a `Processed { success: false }` message-queue event.
5. Attempting `claim_rewards_to` again returns `Error::NoRewardForRelayer` — the relayer has no way to recover the reward through the pallet, despite no successful settlement having occurred. [1](#0-0) [3](#0-2) [2](#0-1)

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-139)
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
