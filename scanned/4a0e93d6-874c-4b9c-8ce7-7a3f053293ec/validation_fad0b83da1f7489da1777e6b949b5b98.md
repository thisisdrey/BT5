Based on the evidence gathered, I found a real analog: the `CallContract` command in Snowbridge's outbound-queue v2, where the `value` field (drawing on the agent contract's own held ether) is validated only for a lower bound, not bound to the assets the origin actually deposited in *this* message.

### Title
Snowbridge outbound-queue v2 `CallContract` lets an origin drain its agent's pre-existing Ether balance beyond the assets transferred in the message - ([File: bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs])

### Summary
`XcmConverter::convert` in the outbound-queue v2 converter parses a user-supplied `Transact` instruction into a `ContractCall::V1 { target, calldata, value, gas }`, which becomes `Command::CallContract` executed by the Gateway/agent contract on Ethereum [1](#0-0) . The `value` field is documented as "Include ether held by the agent contract" [2](#0-1) , meaning the origin can direct the agent to spend Ether the agent already holds from *prior, unrelated* messages/settlements — not just the Ether being transferred in the current XCM. The only check found (`CallContractValueInsufficient`) validates a lower bound relationship, and the integration test comment itself acknowledges this: "value should be less than the transfer amount, require validation on BH Exporter" [3](#0-2) , implying the constraint is a comparison against this message's transfer, not an actual accounting of the agent's real on-chain Ether balance.

### Finding Description
The `target` and `calldata` fields of `ContractCall::V1` are fully attacker-controlled (any account able to submit a `PolkadotXcm::execute`/`send` with a `Transact` instruction routed to Ethereum) [4](#0-3) . This is structurally identical to the `ZeroExAdapter` bug: a public-facing wrapper forwards attacker-supplied destination/calldata straight to an external execution engine, with only a same-message balance-style check as a guard, while the *actual pool of value* it can draw from (the agent's Ether balance) is not what is validated. The `extract_remote_fee`, `extract_ethereum_native_assets`, and `extract_polkadot_native_assets` helpers validate that assets declared in *this* message add up correctly [5](#0-4) , but none of them cross-check the `value` field used in the `CallContract` command against the agent's actual, cumulative Ether reserve. Since `value` explicitly "includes ether held by the agent contract" (i.e., pre-existing balance from past reward payouts, refunds, or previous transfers), a caller who controls (or aliases into, subject to `AllowedAliasOrigin`) an agent with accumulated Ether can direct that Ether to an arbitrary Ethereum `target` via arbitrary `calldata`, exactly mirroring the 0x adapter's unchecked "recipient"/"target" parameter that let an attacker redirect purchased/held tokens away from the intended owner.

### Impact Explanation
If the value bound is enforced only against the current message's transferred amount (as the test comment states) rather than being reconciled against the agent's real historical accrued balance on Ethereum, an origin can repeatedly queue small legitimate transfers while draining previously accumulated agent Ether to a `target`/`calldata` of their choosing — effectively unbacked withdrawal of bridge-held funds to an attacker address. This matches the "theft or unbacked mint or unlock" and "public underpriced work" impact categories for Snowbridge/BridgeHub in scope.

### Likelihood Explanation
Likelihood is moderate: it requires an origin authorized to `Transact` for an agent (its own agent, or one it can alias into subject to `AllowedAliasOrigin`) that has accumulated non-trivial Ether over time, and requires that the referenced Exporter-side "value validation" is indeed the shallow comparison implied by the test comment rather than a true balance reconciliation against the agent's on-chain reserve — this could not be fully confirmed from the converter code alone, only inferred from the primitive's fields and the test's own caveat.

### Recommendation
Validate `Command::CallContract.value` against the agent's tracked, on-chain-verifiable Ether balance (not just against the current message's transfer amount), or disallow `value > 0` combined with `Transact`/arbitrary `target`/`calldata` unless the calling origin is proven to be the sole owner of all Ether ever credited to that agent. Add an explicit invariant test that repeatedly funds an agent, then submits a minimal-transfer message with `value` claiming the full historical agent balance directed at an attacker-controlled `target`, to confirm whether the current `CallContractValueInsufficient` check actually catches this case end-to-end on BridgeHub's exporter.

### Proof of Concept
Not independently executed — this would need to be validated by a background agent against BridgeHub's Exporter/`OutboundQueueV2` pallet logic (outside what was retrievable via search) to confirm whether `value` is checked against the agent's cumulative on-chain Ether balance or only against the current message's declared transfer, as the existing test suite's own comment suggests the latter. This is a limitation of the available indexed code and should be confirmed by a Devin session with full repository/build access.


Repository: Kohvert/polkadot-sdk--033

Context: The Snowbridge outbound-queue v2 pipeline converts an XCM `Transact` instruction into a `ContractCall::V1 { target, calldata, value, gas }` (see `bridges/snowbridge/primitives/outbound-queue/src/v2/mod.rs` and `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`), which becomes `Command::CallContract` (`bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs`) executed by the agent contract on Ethereum. The doc comment states `value` may "include ether held by the agent contract" — i.e., Ether accumulated from prior, unrelated messages, not just the current transfer. An integration test comment (`cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs`, around the `transact_with_agent_from_asset_hub` test) explicitly notes "value should be less than the transfer amount, require validation on BH Exporter" — suggesting the actual enforcement point is in the BridgeHub outbound-queue-v2 pallet/exporter code, not the converter.

Task:
1. Locate the exact BridgeHub outbound-queue-v2 pallet/exporter code path that validates `Command::CallContract.value` (search `bridges/snowbridge/pallets/outbound-queue-v2/` and any `Exporter`/`send`/`validate`/`deliver` implementations) to determine exactly what `value` is checked against.
2. Determine whether the check is (a) a true reconciliation against the agent's actual, cumulative on-chain Ether balance (safe), or (b) only a comparison against the amount transferred within the *current* XCM message (unsafe — allows draining previously accumulated agent Ether to an attacker-chosen `target`/`calldata` over multiple messages).
3. If (b) is confirmed: implement a fix that tracks/reconciles the agent's actual Ether balance (e.g., via a running ledger updated on every inbound/outbound settlement affecting the agent, or by requiring `value` to not exceed the Ether asset amount explicitly included in the *same* message's `WithdrawAsset`/reserve instructions with no carry-over of previously accrued balance) so that an origin cannot use `Transact` + `CallContract` to redirect Ether beyond what it is depositing/using in that specific message.
4. Add a regression test (extending the existing `transact_with_agent_from_asset_hub`-style tests in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs`) that: funds an agent with Ether across multiple prior messages, then submits a subsequent message with a minimal Ether transfer but a `CallContract.value` claiming the agent's full accumulated balance directed at an attacker-controlled `target`, and asserts the message is rejected/fails validation rather than being queued for delivery to Ethereum.
5. Document the finding and fix in a prdoc entry per repository convention if a fix is applied.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L94-186)
```rust
	/// Extract the fee asset item from PayFees(V5)
	fn extract_remote_fee(&mut self) -> Result<u128, XcmConverterError> {
		use XcmConverterError::*;
		let reserved_fee_assets = match_expression!(self.next()?, WithdrawAsset(fee), fee)
			.ok_or(WithdrawAssetExpected)?;
		ensure!(reserved_fee_assets.len() == 1, AssetResolutionFailed);
		let reserved_fee_asset =
			reserved_fee_assets.inner().first().cloned().ok_or(AssetResolutionFailed)?;
		let (reserved_fee_asset_id, reserved_fee_amount) = match reserved_fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		let fee_asset =
			match_expression!(self.next()?, PayFees { asset: fee }, fee).ok_or(InvalidFeeAsset)?;
		let (fee_asset_id, fee_amount) = match fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, *amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		// Check the fee asset is Ether (XCM is evaluated in Ethereum context).
		ensure!(fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_amount >= fee_amount, InvalidFeeAsset);
		Ok(fee_amount)
	}

	/// Extract ethereum native assets
	fn extract_ethereum_native_assets(
		&mut self,
		enas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		for ena in enas.clone().into_inner().into_iter() {
			// Check the the deposit asset filter matches what was reserved.
			if !deposit_assets.matches(&ena) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// only fungible asset is allowed
			let (token, amount) = match ena {
				Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
					match inner_location.unpack() {
						(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
							Ok((H160(*key), amount))
						},
						// To allow ether
						(0, []) => Ok((H160([0; 20]), amount)),
						_ => Err(AssetResolutionFailed),
					}
				},
				_ => Err(AssetResolutionFailed),
			}?;

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			commands.push(Command::UnlockNativeToken { token, recipient, amount });
		}
		Ok(commands)
	}

	/// Extract polkadot native assets
	fn extract_polkadot_native_assets(
		&mut self,
		pnas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		ensure!(pnas.len() > 0, NoReserveAssets);
		for pna in pnas.clone().into_inner().into_iter() {
			if !deposit_assets.matches(&pna) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// Only fungible is allowed
			let Asset { id: AssetId(asset_id), fun: Fungible(amount) } = pna else {
				return Err(AssetResolutionFailed);
			};

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			// Ensure PNA already registered
			let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
			let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
			ensure!(asset_id == expected_asset_id, InvalidAsset);

			commands.push(Command::MintForeignToken { token_id, recipient, amount });
		}
		Ok(commands)
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-304)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/mod.rs (L17-31)
```rust
/// The `XCM::Transact` payload for calling arbitrary smart contracts on Ethereum.
/// On Ethereum, this call will be dispatched by the agent contract acting as a proxy
/// for the XCM origin.
#[derive(Clone, Encode, Decode, PartialEq, Debug, TypeInfo)]
pub enum ContractCall {
	V1 {
		/// Target contract address
		target: [u8; 20],
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Include ether held by the agent contract
		value: u128,
		/// Maximum gas to forward to target contract
		gas: u64,
	},
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L543-549)
```rust
		let transact_info = ContractCall::V1 {
			target: Default::default(),
			calldata: vec![],
			gas: 40000,
			// value should be less than the transfer amount, require validation on BH Exporter
			value: 4 * (TOKEN_AMOUNT - REMOTE_FEE_AMOUNT_IN_ETHER) / 5,
		};
```
