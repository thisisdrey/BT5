## Analysis

The Halborn finding's core broken invariant is: **an accounting counter is decremented to reflect a "settlement" event, but the code accepts an unverified signal (an untrusted external call's return value) as proof that the settlement actually happened**, permanently losing the linkage between the deducted amount and any real transfer of value.

The closest local analog is in the Snowbridge relayer-reward payout path on BridgeHub.

`pallet_bridge_relayers::do_claim_rewards` (a permissionless, signed extrinsic reachable via `claim_rewards_to`) takes the caller's accumulated `RelayerRewards` entry, and treats a successful return from `T::PaymentProcedure::pay_reward` as proof that the reward was paid, permanently removing the record via `try_mutate_exists`/`maybe_reward.take()`: [1](#0-0) 

For the `BridgeReward::Snowbridge` case, `pay_reward` is `PayAccountOnLocation::pay_reward`, which does **not** transfer any value locally — it only builds and *sends* an `UnpaidExecution` XCM to AssetHub (`ReserveAssetDeposited` + `DepositAsset`) and returns `Ok(())` as soon as local validation, fee-charging, and message delivery to the outbound queue succeed: [2](#0-1) 

This mirrors the reported bug class exactly: the local bookkeeping state (`RelayerRewards`) is finalized/erased based on a signal ("XCM accepted for delivery") that does not guarantee the actual effect (asset minted to beneficiary on AssetHub). If the XCM later fails to execute on AssetHub — e.g. the `Barrier` rejects the `UnpaidExecution` for that specific origin path, the `beneficiary` `Location` supplied by the relayer in `BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)` doesn't resolve to a valid deposit target, or the reserve-asset trust configuration on AssetHub is not (or no longer) aligned with the `UniversalOrigin(GlobalConsensus(Ethereum))`/`DescendOrigin(InboundQueueV2Location)` path used — the assets are trapped or the message is dropped on AssetHub, while BridgeHub has already irreversibly deleted the reward entry. The relayer permanently loses their reward with no way to re-claim, since `RelayerRewards` no longer contains the entry and there is no receipt/callback loop that re-credits BridgeHub on remote failure.

This directly violates the stated pivot: *"Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."* Here, payout state (`RelayerRewards`) advances (is cleared) merely on successful **send**, not on successful **execution/settlement** on the destination chain.

### Title
Snowbridge relayer reward is irreversibly cleared on successful XCM *send* rather than confirmed settlement on AssetHub — (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`pallet_bridge_relayers::claim_rewards_to` deletes a relayer's `RelayerRewards` entry as soon as `PayAccountOnLocation::pay_reward` returns `Ok(())`. That implementation only validates, charges local fees, and enqueues an `UnpaidExecution` XCM to AssetHub — it never confirms that the destination chain actually executed `ReserveAssetDeposited`/`DepositAsset` and credited the beneficiary.

### Finding Description
`do_claim_rewards` uses `RelayerRewards::<T, I>::try_mutate_exists` to atomically take the reward balance and call the configured `PaymentProcedure::pay_reward`; on `Ok(())` the reward record is gone for good [3](#0-2) . For the Snowbridge reward kind, `pay_reward` is implemented purely as a "fire-and-forget" cross-chain send: `validate_send`, `charge_fees`, `deliver` — with success meaning only "handed off to the local XCM sender," not "executed and settled remotely" [2](#0-1) . No message id/receipt is retained on BridgeHub to later verify or retry settlement if AssetHub fails to process the message (e.g. barrier rejection, trap due to bad `beneficiary`, misconfigured reserve trust). The existing guard (`try_mutate_exists` rolling back on `Err`) only protects against *local* failures caught synchronously; it cannot detect or roll back on *asynchronous remote* failures because the local call already returned `Ok(())`.

### Impact Explanation
A relayer's earned Snowbridge reward can be permanently and irrecoverably lost from `RelayerRewards` even though no funds were ever credited to the beneficiary, because the local dispatch cannot observe or react to the destination chain's execution outcome. This is a permanent user-fund loss / broken settlement-atomicity bug in the bridge reward-payout flow, matching the impact class "duplicate settlement or payout, permanent user-fund or bridge-state lock."

### Likelihood Explanation
This can be triggered simply by normal use of `claim_rewards_to` combined with any condition that causes the destination-side XCM to fail post-send (barrier/config drift, malformed but well-formed-looking `VersionedLocation` beneficiary, or reserve-trust misconfiguration) — no privileged actor, relayer collusion, or malicious infrastructure is required; it is an architectural gap between local "send success" and remote "settlement success."

### Recommendation
Do not treat XCM `deliver()` success as final settlement. Either (a) retain the reward record (or a pending/receipt marker) until a confirmed execution report/callback from AssetHub is received, using a two-phase commit similar to `PayOverXcm`'s `check_payment`/query-id pattern, or (b) only clear `RelayerRewards` after an explicit success acknowledgment, with a recovery path to re-credit the reward if the remote message fails or is trapped.

### Proof of Concept
1. Relayer accrues reward via `register_reward` for `BridgeReward::Snowbridge`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(some_location))`.
3. `PayAccountOnLocation::pay_reward` validates/charges fees/delivers the XCM locally and returns `Ok(())`; `do_claim_rewards` deletes `RelayerRewards` entry [4](#0-3) .
4. On AssetHub, the `UnpaidExecution`/`ReserveAssetDeposited`/`DepositAsset` sequence fails to execute as intended (e.g., barrier disallows unpaid execution for that origin, or `some_location` does not decode to a depositable beneficiary), so the reward is never actually credited.
5. Relayer calls `claim_rewards_to` again and receives `Error::NoRewardForRelayer` — the reward is permanently gone with no funds ever delivered, as demonstrated by the existing test `bridge_rewards_works`, which already documents that `claim_rewards_to` can return `FailedToPayReward`-style failures in unit-test/no-HRMP conditions, confirming send/settlement are decoupled and unguarded [5](#0-4) .

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-300)
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L777-795)
```rust
			let claim_location = VersionedLocation::V5(Location::new(
				1,
				[
					Parachain(1000),
					xcm::latest::Junction::AccountId32 {
						id: account2.clone().into(),
						network: None,
					},
				],
			));
			// In unit tests without proper HRMP channel setup, the claim will fail at XCM sending.
			assert_err!(
				BridgeRelayers::claim_rewards_to(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge,
					BridgeRewardBeneficiaries::AssetHubLocation(claim_location)
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);
```
