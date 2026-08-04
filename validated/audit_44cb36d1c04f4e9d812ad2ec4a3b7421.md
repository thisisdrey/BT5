### Title
Unbacked relayer reward inflation via attacker-controlled `relayer_fee` field in Snowbridge Inbound Queue V2 - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
The AlignedLayer bug's core broken invariant is: an unprivileged, attacker-controlled pricing parameter (`tx.gasprice`, settable by a user who is also the block producer) is trusted verbatim to compute a monetary payout from a shared balance pot, with no on-chain check tying it to the real cost incurred. The same broken invariant exists in `EthereumInboundQueueV2::process_message` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`), where the `relayer_fee` field of an inbound `Message` — a value fully controlled by whoever calls the Gateway contract on Ethereum — is passed straight through to `RewardPayment::register_reward()` with no validation against the amount of value/assets actually locked and bridged in that same message.

### Finding Description
`Message` (`bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs`, lines 100‑120) is decoded from an Ethereum `OutboundMessageAccepted` log emitted by the Gateway contract when a user calls `v2_sendMessage`/`v2_registerToken`. The relevant fields: [1](#0-0) 

are all populated straight from `event_payload` fields (`origin.value`, `executionFee`, `relayerFee`) with no cross-validation between them: [2](#0-1) 

Once a relayer submits the light-client proof for this event via `submit`, `process_message` runs: [3](#0-2) 

The only checks performed are: the gateway address matches (`InvalidGateway`) and the nonce hasn't been used (`InvalidNonce`). There is **no check that `relayer_fee` is bounded by, or backed by, the `value`/`assets` actually carried by the message**. `relayer_fee` is an arbitrary `u128` chosen by whoever called the Gateway contract on Ethereum — an unprivileged, permissionless action requiring no relayer/validator/governance role. `register_reward` (`bridges/modules/relayers/src/lib.rs`, `register_relayer_reward`) then unconditionally credits `RelayerRewards` storage with this amount: [4](#0-3) 

This registered balance is later paid out through `claim_rewards_to` → `BridgeRewardPayer::pay_reward` → `PayAccountOnLocation::pay_reward` (`cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs`, lines 117‑139), which moves real Ether-denominated value out of the bridge's shared Ether pot on Asset Hub to the claiming relayer: [5](#0-4) 

Because the same actor can trivially be both the message sender on Ethereum (who freely sets `relayerFee` in the Gateway call) and the relayer who submits the proof and claims the reward on Bridge Hub, this is structurally identical to the "user == miner" scenario in the AlignedLayer report: a self-controlled fee parameter is used unmodified to calculate a payout drawn from a pool that is meant to cover costs across all bridge users, not just the amount this specific message actually backed.

### Impact Explanation
Any account can submit an Ethereum transaction to the Gateway contract with an arbitrarily large `relayerFee` (bounded only by whatever the contract itself enforces on Ethereum, if anything, since this parameter is a fee field the sender chooses to promise for relaying, not collateral that must be pre-funded 1:1 with locked Ether transferred in the same message) and then relay their own message to claim that reward. If the Ethereum-side Gateway does not strictly lock/escrow `relayerFee` Ether alongside `value`/`executionFee` for every message (the pallet's `process_message` performs no such reconciliation), an attacker can register unbacked reward claims against the shared Ether/WETH pot backing Snowbridge rewards on Asset Hub, draining funds intended for honest relayers/users — a direct value-conservation violation ("theft or unbacked mint/unlock" per the impact gate).

### Likelihood Explanation
The path requires no privileged role: submitting a message via the public Gateway contract and later calling the permissionless `submit`/`process_message` flow are both open to any account. The only barrier to exploitation is whether the off-chain Ethereum Gateway contract enforces `relayerFee` to be pre-funded/escrowed 1:1 in the same transaction — a constraint that lives entirely outside this repository and is not re-verified on the Polkadot-SDK side. Given the pallet performs zero on-chain validation of `relayer_fee` against `value`/`assets`, this is a real gap in defense-in-depth: the runtime blindly trusts an attacker-suppliable number as a monetary amount to credit.

### Recommendation
In `EthereumInboundQueueV2::process_message`, do not trust `message.relayer_fee` as an unconstrained payout amount. Cross-validate it against the Ether/asset value actually accompanying the message (e.g., require `relayer_fee + execution_fee <= value`, or derive the payable reward from assets actually extracted/held for this nonce rather than from a raw sender-supplied field), and/or cap the reward to a governance-configured maximum per message. Consider also enforcing (or documenting and auditing) that the Ethereum Gateway contract escrows `relayerFee` funds atomically with `value`, and add an explicit invariant check on the Substrate side rather than relying solely on off-chain contract guarantees.

### Proof of Concept
1. Attacker deploys/controls an EOA on Ethereum and calls the Gateway contract's `v2_sendMessage`, setting `relayerFee = MAX_ETH` (or any inflated value far beyond what is actually escrowed/backed) while keeping `value`/`assets` at some low/zero size, producing an `OutboundMessageAccepted` event.
2. The same attacker (or a colluding account) waits for the message to finalize and submits the `submit` extrinsic on Bridge Hub with the corresponding proof, invoking:
   `EthereumInboundQueueV2::process_message(relayer = attacker, message)` — see [6](#0-5) 
3. `total_tip = relayer_fee.saturating_add(tip)` is computed purely from the attacker-controlled `relayer_fee` and registered via `T::RewardPayment::register_reward(&relayer, ..., total_tip)` with no check against `message.value`/`message.assets`.
4. Attacker calls `pallet_bridge_relayers::claim_rewards_to` to redeem the inflated `RelayerRewards` balance, causing `BridgeRewardPayer::pay_reward` → `PayAccountOnLocation::pay_reward` to transfer real Ether-denominated funds from the shared bridge reward pot to the attacker — see [5](#0-4) 
5. Test scaffolding in the repo confirms the reward amount registered is taken verbatim from the `relayer_fee` field of the message with no independent validation: [7](#0-6) 

Note: I was unable to fully confirm within this session whether the off-chain Ethereum `GatewayV2` contract enforces that `relayerFee` is escrowed 1:1 alongside `value` at send time (that contract code is outside this repository's Rust source and outside my tool access). If such enforcement exists and is airtight, the practical exploitability is reduced to a defense-in-depth gap; if it does not, this is a directly exploitable fund-drain path. This uncertainty should be resolved by inspecting the Solidity `GatewayV2` contract logic for `v2_sendMessage`/`v2_registerToken`.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L108-120)
```rust
	/// The assets sent from Ethereum (ERC-20s).
	pub assets: Vec<EthereumAsset>,
	/// The command originating from the Gateway contract.
	pub payload: Payload,
	/// The claimer in the case that funds get trapped. Expected to be an XCM::v5::Location.
	pub claimer: Option<Vec<u8>>,
	/// Native ether bridged over from Ethereum
	pub value: u128,
	/// Fee in eth to cover the xcm execution on AH.
	pub execution_fee: u128,
	/// Relayer reward in eth. Needs to cover all costs of sending a message.
	pub relayer_fee: u128,
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L166-179)
```rust
		let message = Message {
			gateway: log.address,
			nonce: event.nonce,
			origin: H160::from(event_payload.origin.as_ref()),
			assets: substrate_assets,
			payload: message_payload,
			claimer,
			value: event_payload.value,
			execution_fee: event_payload.executionFee,
			relayer_fee: event_payload.relayerFee,
		};

		Ok(message)
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```

**File:** bridges/modules/relayers/src/lib.rs (L399-431)
```rust
		/// Register reward for given relayer.
		pub(crate) fn register_relayer_reward(
			reward_kind: T::Reward,
			relayer: &T::AccountId,
			reward_balance: T::RewardBalance,
		) {
			if reward_balance.is_zero() {
				return;
			}

			RelayerRewards::<T, I>::mutate(
				relayer,
				reward_kind,
				|old_reward: &mut Option<T::RewardBalance>| {
					let new_reward =
						old_reward.unwrap_or_else(Zero::zero).saturating_add(reward_balance);
					*old_reward = Some(new_reward);

					tracing::trace!(
						target: crate::LOG_TARGET,
						?relayer,
						?reward_kind,
						?new_reward,
						"Relayer can now claim reward for serving payer"
					);

					Self::deposit_event(Event::<T, I>::RewardRegistered {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
					});
				},
			);
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs (L441-484)
```rust
#[test]
fn relayer_fee_paid_out_when_no_tip_exists() {
	new_tester().execute_with(|| {
		let nonce: u64 = 88;
		let relayer_fee: u128 = 5_000;

		// Ensure no tip exists for this nonce
		assert_eq!(Tips::<Test>::get(nonce), None);

		// Process inbound message with relayer_fee but no tip
		let relayer: AccountId = Keyring::Bob.into();
		assert_ok!(InboundQueue::process_message(
			relayer,
			Message {
				nonce,
				assets: vec![],
				payload: Payload::Raw(vec![]),
				claimer: None,
				execution_fee: 1_000_000_000,
				relayer_fee,
				gateway: mock::GatewayAddress::get(),
				origin: H160::random(),
				value: 3_000_000_000,
			},
		));

		// Relayer fee should be paid out even without tip
		assert_eq!(
			RegisteredRewardsCount::get(),
			1,
			"Relayer fee should be paid out even when no tip exists"
		);

		// Check the actual reward amount paid out
		assert_eq!(
			RegisteredRewardAmount::get(),
			relayer_fee,
			"Reward amount should equal relayer_fee when no tip exists"
		);

		// Confirm no tip storage was affected
		assert_eq!(Tips::<Test>::get(nonce), None);
	});
}
```
