Based on my analysis, I found a genuine local analog to the Winnables bug class. The core broken invariant in the external report is: a public entrypoint accepts unvalidated user-supplied routing parameters (destination/beneficiary), the caller has no way to guarantee the underlying cross-chain message will actually reach and be executed correctly at the destination, and once the message is *accepted for sending* the on-chain reward/prize accounting is irreversibly consumed — so a wrong-but-"valid-looking" beneficiary silently burns the reward with no recovery path.

### Title
`claim_rewards_to` permanently burns a relayer's Snowbridge reward when the caller-supplied `AssetHubLocation` beneficiary is unroutable/wrong but still passes local XCM `validate` - (File: `bridges/modules/relayers/src/lib.rs`)

### Summary
`pallet_bridge_relayers::Pallet::claim_rewards_to` lets any signed relayer supply an arbitrary `beneficiary: BeneficiaryOf<T, I>` (for Snowbridge rewards this is `BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)`), and consumes/removes the relayer's accrued reward from `RelayerRewards` storage as part of the same atomic mutation that calls `PayAccountOnLocation::pay_reward`. `pay_reward` only fails (and thus only reverts the storage removal) if XCM `validate`, `charge_fees`, or local `deliver` fail *synchronously on this chain*. It does **not** verify that the destination `Location` is actually a valid/existing account or that AssetHub will successfully execute the `DepositAsset` instruction. If the relayer (accidentally or via a griefer front-running with a bad beneficiary is not even required — the caller controls it directly) supplies a syntactically valid but semantically wrong `Location` (wrong account encoding, non-existent junctions, or a destination that AssetHub's XCM executor cannot resolve to a real account), the local `pay_reward` call returns `Ok(())`, `try_mutate_exists` commits, `RelayerRewards` entry is deleted, and the `RewardPaid` event fires — while the remote `DepositAsset` on AssetHub silently fails or deposits into an unreachable/wrong location. The reward is unbacked-minted on the wrong side or simply lost, and the relayer has no way to reclaim it since `RelayerRewards` no longer has an entry (`NoRewardForRelayer` on retry).

### Finding Description
The call chain is:
1. `claim_rewards_to(origin, reward_kind, beneficiary)` → `Self::do_claim_rewards(relayer, reward_kind, beneficiary)` [1](#0-0) 
2. `do_claim_rewards` uses `RelayerRewards::try_mutate_exists` — `reward_balance = maybe_reward.take()` deletes the storage entry **before** the payment result is known, then calls `T::PaymentProcedure::pay_reward(...)`; only an `Err` returned synchronously from `pay_reward` rolls the mutation back [2](#0-1) 
3. For `BridgeReward::Snowbridge`, the beneficiary variant `AssetHubLocation(VersionedLocation)` is decoded from a `VersionedLocation` supplied entirely by the caller and passed straight into `PayAccountOnLocation::pay_reward` with no chain/route/account validation beyond XCM version conversion [3](#0-2) 
4. `PayAccountOnLocation::pay_reward` builds an XCM (`DepositAsset { ..., beneficiary }`), validates it against the local `XcmSender`, charges local fees, and calls `deliver` — returning `Ok(())` as soon as the message is handed off to the outbound queue. It never confirms that `beneficiary` resolves to a real, reachable account on AssetHub, nor that the `DepositAsset` will actually execute successfully there [4](#0-3) 

This exactly mirrors the Winnables bug shape: a public-facing function takes attacker/user-controlled routing/destination parameters, changes irreversible local accounting state as soon as local acceptance checks pass, and dispatches a cross-chain message whose remote success is never confirmed before or via that state change. The existing `try_mutate_exists` "guard" only catches local synchronous failures (bad XCM version, no route configured, fee charge failure) — it cannot and does not catch the class of errors that only manifest on the remote chain (bad beneficiary encoding that XCM `validate`/local `deliver` cannot detect, e.g. a `Location` whose junctions don't correspond to any real account convertible by AssetHub's `LocationToAccountId`).

### Impact Explanation
This is a "theft or unbacked mint or unlock" / "permanent user-fund lock" class impact per the required-impacts gate: the relayer's legitimately earned bridge reward balance is deleted from `RelayerRewards` (so it can never be reclaimed, `NoRewardForRelayer` on any retry) while the actual value transfer to a real beneficiary account never happens, because the remote-side failure occurs after the point of no return on the source chain. Funds are effectively burned/lost with no recovery mechanism — a permanent value-conservation violation matching the "must conserve value and settle exactly once to the rightful beneficiary" pivot.

### Likelihood Explanation
Likelihood is high because the beneficiary is fully attacker/user-controlled and `claim_rewards_to` is an unprivileged, unauthenticated-destination signed extrinsic — any relayer can trigger this on themselves accidentally (fat-fingered location encoding), and it requires no malicious peer, validator, collator, or governance action, satisfying the "unprivileged attacker" and "public entrypoint" requirement of the task. The only barrier is that the relayer must already have accrued a nonzero reward, which is a normal, expected precondition, not a privileged one.

### Recommendation
Do not remove/settle the `RelayerRewards` entry until remote delivery is confirmed, or restructure the flow so that beneficiary validity (destination decodability into a real account per AssetHub's `LocationToAccountId`) is checked before consuming the local reward record. At minimum, add a local-side sanity check in `PayAccountOnLocation::pay_reward` (or in `BridgeRewardPayer::pay_reward`) that the supplied `Location`/`VersionedLocation` beneficiary can be converted to a valid AssetHub account before allowing `do_claim_rewards` to commit removal of the reward, and/or maintain a pending/unsettled reward state keyed by XCM message id that is only cleared upon a confirmed delivery/execution receipt (mirroring the `PendingOrders`/delivery-receipt pattern already used in `outbound-queue-v2`).

### Proof of Concept
1. Relayer accrues a `BridgeReward::Snowbridge` reward via `register_reward`, confirmed by `relayer_reward(&account2, BridgeReward::Snowbridge)`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(claim_location))` where `claim_location` is a syntactically valid `VersionedLocation` (e.g. wrong `AccountId32` junction, or wrong parachain), which the mock/local XCM sender accepts (`validate`/`deliver` return `Ok`).
3. `do_claim_rewards`'s `try_mutate_exists` commits: `RelayerRewards` entry is removed, `RewardPaid` event fires, `pay_reward` returned `Ok(())`.
4. On AssetHub, the corresponding `DepositAsset { beneficiary, ... }` instruction fails/executes against an unintended account because the destination is unreachable/incorrect — reward value is not delivered to the intended relayer.
5. Relayer calls `claim_rewards`/`claim_rewards_to` again → fails with `Error::<T, I>::NoRewardForRelayer`, confirming the reward is permanently gone with no recovery path.

This mirrors the existing repo test `bridge_rewards_works` at [5](#0-4) , where an even simpler HRMP-unset misconfiguration already demonstrates that the "unroutable/incorrect beneficiary" case is only caught by accident (missing test infra), not by a real invariant check on the beneficiary/destination correctness.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L224-235)
```rust
		/// Claim accumulated rewards and send them to the alternative beneficiary.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::claim_rewards_to())]
		pub fn claim_rewards_to(
			origin: OriginFor<T>,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer, reward_kind, beneficiary)
		}
```

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-135)
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
