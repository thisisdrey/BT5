## Analysis

The core broken invariant in the external report is: **a state-mutating action is finalized (and cannot be reversed) even though the actual value transfer to the intended recipient can fail**, because the code that "settles" the payout does not verify that the recipient-side operation actually succeeded (BGT transfer succeeds locally in one step but stalls/fails downstream at the farming contract, leaving funds stuck with no way back).

The closest local analog in this repository is the Snowbridge relayer-reward claim path, where the reward-claiming pallet marks a reward as paid and deletes the ledger entry based solely on the **local XCM send succeeding**, without any acknowledgement that the remote `DepositAsset` on AssetHub actually completed.

### Relevant code

`do_claim_rewards` removes the reward entry from storage only if `pay_reward` returns `Ok`, and this is the only gate for permanently discarding the claim: [1](#0-0) 

`PayAccountOnLocation::pay_reward` builds an unpaid, fire-and-forget XCM (`ReserveAssetDeposited` + `DepositAsset`) targeting AssetHub, and returns `Ok(())` as soon as `XcmSender::deliver(ticket)` succeeds — i.e. once the message is queued for local dispatch, not once the remote mint/deposit is confirmed: [2](#0-1) 

The unit tests for this type only cover local failure modes (`validate`/`charge_fees`/`deliver` failing) — there is no test, and no code path, that accounts for the remote `DepositAsset` failing/trapping on AssetHub after local delivery succeeds: [3](#0-2) 

### Title
Relayer reward ledger entry is irreversibly deleted before confirming the cross-chain asset deposit actually succeeds - (File: `bridges/modules/relayers/src/lib.rs`, `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`pallet-bridge-relayers::claim_rewards_to` calls `BridgeRewardPayer::pay_reward`, which for `BridgeReward::Snowbridge` delegates to `snowbridge_core::reward::PayAccountOnLocation::pay_reward`. That function considers the payout "successful" as soon as the constructed XCM (`ReserveAssetDeposited` + `DepositAsset`) is handed off to the local XCM sender (`XcmSender::deliver`). It has no mechanism to learn whether the remote `DepositAsset` on AssetHub actually executes. `do_claim_rewards` treats `pay_reward`'s `Ok` as final settlement and permanently removes the `RelayerRewards` entry via `try_mutate_exists`, emitting `RewardPaid`.

### Finding Description
The flow is:
1. `claim_rewards_to` → `do_claim_rewards` reads and, inside `try_mutate_exists`, removes the relayer's accumulated reward from `RelayerRewards<T, I>` — but only commits this removal if the inner closure returns `Ok`.
2. The closure calls `T::PaymentProcedure::pay_reward(...)`, which for Snowbridge routes to `PayAccountOnLocation::pay_reward`.
3. `pay_reward` builds an `UnpaidExecution` XCM containing `ReserveAssetDeposited` (treating BridgeHub as the reserve for the bridged Ethereum asset) followed by `DepositAsset { beneficiary, .. }`, targeted at AssetHub, and returns `Ok(())` once `validate_send` and `XcmSender::deliver` succeed — i.e., once the message is accepted into the local transport queue.
4. There is no `SetAppendix`/`ReportError`/callback wired into this XCM, so BridgeHub never learns whether AssetHub's execution of `ReserveAssetDeposited`/`DepositAsset` actually succeeds.
5. Because step 1 already deleted the ledger entry once `pay_reward` returned `Ok`, if the remote deposit later fails (e.g. the beneficiary account cannot receive/hold the foreign asset — non-existent account with the asset not marked `IsSufficient`, a frozen/blocked beneficiary, or any other AssetHub-side rejection of `DepositAsset`), the relayer's reward is permanently lost: it cannot be re-claimed, and no compensating mint occurs.

This mirrors the BGT report's root cause precisely: an action is treated as "done" and its associated bookkeeping is irreversibly advanced based on local success, while the actual value transfer/acceptance depends on a downstream party/rule (a receiving contract's whitelist in the BGT case; a receiving AssetHub account's asset-acceptance rules here) that is not verified before finalizing.

### Impact Explanation
This causes silent, permanent loss of legitimately earned relayer rewards with no recovery path — a violation of "payout state must only advance after ... execution and settlement succeed atomically" and "settle exactly once to the rightful beneficiary and amount." Because relayer incentives are central to keeping Snowbridge message delivery running, repeated loss of rewards degrades relayer participation and can stall bridge processing over time.

### Likelihood Explanation
Any relayer using `claim_rewards_to` with `BridgeRewardBeneficiaries::AssetHubLocation` can trigger this without needing any privileged, malicious-peer, or admin conditions — an unprivileged, ordinary user action (choosing/typo-ing a beneficiary account that AssetHub's asset rules will reject) is sufficient to permanently lose the reward. No malicious relayer, validator, or governance actor is required.

### Recommendation
Do not finalize/delete the `RelayerRewards` entry purely on local XCM dispatch success. Options:
- Use a reserve-and-confirm pattern: keep the reward pending until an explicit success acknowledgment (e.g., `ReportError`/`SetAppendix` reporting instruction, or a receipt callback from AssetHub) confirms the deposit executed.
- Alternatively, pre-validate that the beneficiary can legitimately receive the asset (existence/ED/sufficiency checks) before committing to remove the ledger entry, and provide a way to re-credit/retry on failure.

### Proof of Concept
1. A relayer accumulates a Snowbridge reward via `register_reward` (as shown in `bridge_rewards_works`/`claim_rewards_works` tests).
2. The relayer calls `claim_rewards_to(BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(location))` where `location` resolves to an AssetHub account that cannot accept the bridged asset (e.g., non-existent account and the asset not configured `IsSufficient`, so `DepositAsset` on AssetHub fails/traps).
3. `PayAccountOnLocation::pay_reward` still returns `Ok(())` because `validate_send`/`deliver` only assert the message was queued locally on BridgeHub — as demonstrated by the passing `pay_reward_success` test, which only mocks local send/deliver and never simulates remote execution.
4. `do_claim_rewards`'s `try_mutate_exists` commits, deleting the `RelayerRewards` entry and emitting `RewardPaid`.
5. On AssetHub, the XCM's `DepositAsset` instruction fails; the relayer never receives the asset, and the reward is permanently unrecoverable since the on-chain ledger no longer records it.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-303)
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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L246-266)
```rust
	#[test]
	fn pay_reward_success() {
		let relayer = MockRelayer(AccountId32::new([1u8; 32]));
		let beneficiary = Location::new(1, Here);
		let reward = 1_000u128;

		type TestedPayAccountOnLocation = PayAccountOnLocation<
			MockRelayer,
			u128,
			EthereumNetwork,
			AssetHubLocation,
			InboundQueueLocation,
			MockXcmSender,
			MockXcmExecutor,
			MockCall,
		>;

		let result = TestedPayAccountOnLocation::pay_reward(&relayer, (), reward, beneficiary);

		assert!(result.is_ok());
	}
```
