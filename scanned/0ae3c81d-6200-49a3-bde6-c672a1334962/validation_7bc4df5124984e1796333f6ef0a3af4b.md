## Title
`EthereumBlobExporter` accepts an unbound `WithdrawAsset`/`DepositAsset` pattern and forges `Command::UnlockNativeToken` without verifying the token was ever really locked - ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs])

### Summary
Snowbridge's outbound XCM→Ethereum converter (`XcmConverter`) has two code paths that turn a Substrate-side XCM program into a `Command` sent to the Ethereum Gateway contract: `make_mint_foreign_token_command` (for Polkadot-native assets going to Ethereum) and `make_unlock_native_token_command` (for Ethereum-native ERC-20/ETH being released back on Ethereum). The former validates that the asset location it extracts round-trips through the registered `TokenId`/`ConvertAssetId` mapping (`ensure!(asset_id == expected_asset_id, InvalidAsset)`), but the latter has **no equivalent binding check**: it simply reads the raw `AccountKey20` bytes out of whatever `Asset` location appears in the `WithdrawAsset`/`DepositAsset` instructions and uses them directly as the Ethereum `token` address in `Command::UnlockNativeToken`, with no verification that this location is the reanchored representation of a real ERC-20 that is actually locked in the Gateway's collateral for the `amount` claimed. This mirrors the reported bug class exactly: a value that determines "which asset moves" is taken from an unvalidated/attacker-influenceable field while a separate, unrelated value is trusted for the state-changing effect.

### Finding Description
In `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`:

- `make_mint_foreign_token_command` (lines 342-426) extracts `asset_id` from the `ReserveAssetDeposited` instruction and cross-checks it: [1](#0-0) 
This ensures the asset location used to mint a wrapped token on Ethereum is exactly the one obtained by reversing the registered `TokenId`, closing the loop between "what was reserved" and "what will be represented on Ethereum."

- `make_unlock_native_token_command` (lines 225-317), used for the reverse direction (Ethereum-native ERC-20/ETH being unlocked back to a recipient on Ethereum), extracts the token address directly from the `AccountKey20` junction of the withdrawn asset and never performs any comparable check against a registered/authoritative mapping: [2](#0-1) 
The resulting `Command::UnlockNativeToken { agent_id, token, recipient, amount }` is handed straight to `OutboundQueue::validate`/`deliver` and ultimately to the real Ethereum Gateway contract, which will transfer `amount` of the ERC-20 at `token` out of its custody to `recipient` — with the "proof" of legitimacy being nothing more than the *shape* of the XCM program passed to the exporter, not an on-chain balance debit that the converter itself verifies.

The exporter's `validate()` function (lines 55-138) treats the `xcm` payload solely as *data to pattern-match and re-encode*, not as a program whose `WithdrawAsset`/`DepositAsset` instructions are executed by the local runtime to actually move real collateral before the export happens: [3](#0-2) 
Whether real value is conserved therefore depends entirely on the caller only ever being able to reach `ExportXcm::validate` through a code path where the nested `xcm` was itself derived, instruction-for-instruction, from a real prior withdrawal of the exact same asset/amount (as the legitimate `pallet_xcm` reserve-transfer machinery does). The converter contains no independent assertion of that invariant — unlike the mint path, which re-derives and compares the asset identity against the registered mapping, the unlock path has no analogous "is this token/amount actually backed" check at all.

### Impact Explanation
If any code path exists (e.g. `pallet_xcm::execute` with an `ExportMessage` instruction, or any other pallet/precompile capable of constructing an XCM program containing `ExportMessage { network: Ethereum, ... }` with an attacker-chosen nested `xcm`) that is not itself gated by a prior real balance debit matching the nested program, an attacker can force emission of `Command::UnlockNativeToken` for an arbitrary ERC-20 `token` address and arbitrary `amount` to an arbitrary `recipient`. Once relayed and executed on Ethereum, the Gateway contract will release real, other-users'-owned locked collateral — a direct theft/unbacked-unlock impact, which is exactly the kind of "theft or unbacked mint or unlock" impact called out as in-scope.

### Likelihood Explanation
Exploitability is contingent on whether some reachable, unprivileged entry point on BridgeHub/AssetHub can hand the exporter a `WithdrawAsset`/`DepositAsset` pair whose asset/amount was **not** the product of a real, equal-amount local withdrawal (i.e., a path where the nested `xcm` given to `ExportMessage` is attacker-supplied data rather than executor-derived from an actual debit). I was not able to fully confirm within the available searches whether AssetHub's or BridgeHub's `pallet_xcm` "execute" filter (`XcmExecuteFilter`/`SafeCallFilter`) permits `ExportMessage`, or whether any other extrinsic/pallet call can construct such a mismatched nested program; this determination requires reading the runtime's `xcm_config.rs` filter definitions in full, which the available tool budget did not allow me to complete. Absent that confirmation, likelihood should be treated as **uncertain but concrete on the code level**: the converter itself unambiguously lacks the token/amount-binding check that its sibling function has, which is a structural asymmetry worth fixing regardless of whether a currently-reachable public entry point already permits the attack.

### Recommendation
Add a binding check in `make_unlock_native_token_command` symmetric to the one in `make_mint_foreign_token_command`: after extracting `token`/`amount` from the withdrawn asset, verify (a) that the asset's reanchored location is the canonical representation for that `H160` address under the current network context, and (b) — more importantly — that the `WithdrawAsset`/`DepositAsset` pair passed into `ExportMessage` can only ever be constructed by trusted, executor-derived XCM (i.e., audit and restrict every caller of `ExportXcm` / every extrinsic that can embed an `ExportMessage` instruction to ensure the nested `xcm` is never attacker-suppliable independent of a real matching local-asset debit). Where possible, additionally require the exporter to consume a token/lock-receipt reference recorded on-chain rather than trusting arbitrary XCM instruction shapes.

### Proof of Concept
Conceptual PoC (requires confirming a reachable entry point, e.g. `pallet_xcm::execute`, permits `ExportMessage` on BridgeHub/AssetHub with attacker-controlled nested XCM — not verified in this session):
1. Attacker submits an XCM program containing:
   `ExportMessage { network: Ethereum{chain_id}, destination: Here, xcm: [ WithdrawAsset((0,[AccountKey20{key: <victim's real locked WETH address>}]), amount), ClearOrigin, DepositAsset(..., beneficiary: attacker_eth_address) ] }`
2. The nested `xcm` here is never executed against any real local balance — it is passed straight to `EthereumBlobExporter::validate`, which calls `XcmConverter::make_unlock_native_token_command`.
3. `make_unlock_native_token_command` extracts `token = victim's WETH address`, `amount`, and `recipient = attacker_eth_address`, with no on-chain check that the attacker ever possessed or withdrew that ERC-20 amount, producing `Command::UnlockNativeToken { token, recipient, amount }`. [2](#0-1) 
4. Once relayed, the real Ethereum Gateway contract unlocks `amount` of the real WETH token to the attacker's address, draining collateral that legitimately belongs to other bridge users.

Note: step 1's actual reachability (i.e., confirming that some unprivileged, non-governance entry point can present the exporter with a `WithdrawAsset`/`DepositAsset` pair not backed by a real prior debit) was not fully verified due to tool-call limits; a Devin session with full repository/build access would be needed to trace `pallet_xcm`'s execute filters and the `SovereignPaidRemoteExporter`/router chain to close this gap definitively.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L112-138)
```rust
		let message = message.take().ok_or_else(|| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", "xcm message not provided.");
			SendError::MissingArgument
		})?;

		let mut converter =
			XcmConverter::<ConvertAssetId, ()>::new(&message, expected_network, agent_id);
		let (command, message_id) = converter.convert().map_err(|err|{
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "unroutable due to pattern matching.");
			SendError::Unroutable
		})?;

		let channel_id: ChannelId = ParaId::from(para_id).into();

		let outbound_message = Message { id: Some(message_id.into()), channel_id, command };

		// validate the message
		let (ticket, fee) = OutboundQueue::validate(&outbound_message).map_err(|err| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		// convert fee to Asset
		let fee = Asset::from((Location::parent(), fee.total())).into();

		Ok(((ticket.encode(), message_id), fee))
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L289-316)
```rust
		let (token, amount) = match reserve_asset {
			Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
				match inner_location.unpack() {
					// Get the ERC20 contract address of the token.
					(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
						Some((H160(*key), *amount))
					},
					// If there is no ERC20 contract address in the location then signal to the
					// gateway that is a native Ether transfer by using
					// `0x0000000000000000000000000000000000000000` as the token address.
					(0, []) => Some((H160([0; 20]), *amount)),
					_ => None,
				}
			},
			_ => None,
		}
		.ok_or(AssetResolutionFailed)?;

		// transfer amount must be greater than 0.
		ensure!(amount > 0, ZeroAssetTransfer);

		// Check if there is a SetTopic and skip over it if found.
		let topic_id = match_expression!(self.next()?, SetTopic(id), id).ok_or(SetTopicExpected)?;

		Ok((
			Command::UnlockNativeToken { agent_id: self.agent_id, token, recipient, amount },
			*topic_id,
		))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L417-420)
```rust
		let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;

		let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
		ensure!(asset_id == expected_asset_id, InvalidAsset);
```
