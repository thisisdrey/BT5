Audit Report

## Title
Missing zero-address validation on ENA/PNA recipient in Snowbridge outbound XCM-to-Ethereum converter causes permanent fund loss - (File: `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`, `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

## Summary
The Snowbridge outbound-queue XCM converters extract the `recipient: H160` directly from the `DepositAsset { beneficiary, .. }` XCM instruction's `AccountKey20` junction without ever validating that it is non-zero, in both `XcmConverter::make_unlock_native_token_command` (v1) and `XcmConverter::convert` (v2). Any unprivileged user submitting a normal XCM asset transfer with `beneficiary: AccountKey20 { key: [0u8;20], .. }` will have that zero address embedded unchecked into `Command::UnlockNativeToken`/`Command::MintForeignToken`, which is queued for execution on the Ethereum Gateway contract.

## Finding Description
In v1, the beneficiary match occurs at [1](#0-0) , requiring only that it is a single `AccountKey20` at depth 0 with a matching network — no zero check. The extracted `recipient` flows unchanged into `Command::UnlockNativeToken { agent_id, token, recipient, amount }` at [2](#0-1) . The analogous `make_mint_foreign_token_command` path has the same lack of validation for its beneficiary/recipient.

In v2, the identical pattern-match occurs at [3](#0-2) , and the unchecked `recipient` is passed into both `extract_ethereum_native_assets` (producing `Command::UnlockNativeToken`) and `extract_polkadot_native_assets` (producing `Command::MintForeignToken`) at [4](#0-3)  and [5](#0-4) . The `Command` enum's `recipient: H160` field for both variants is ABI-encoded directly as the Ethereum `address` parameter that the Gateway contract will act on, in both v1 [6](#0-5)  and v2 message formats [7](#0-6) . No `ensure!` or other check comparing `recipient` against `H160::zero()`/`[0u8;20]` exists anywhere along this path in either converter file — confirmed by searching for zero-address guard patterns (`H160::zero`, `is_zero`, `InvalidBeneficiary`, etc.) across the outbound-queue and outbound-queue-v2 crates, which returned no matches in the converter logic itself.

## Impact Explanation
This matches the "permanent user-fund or bridge-state lock" category in the accepted impact set: a zero-value beneficiary causes the corrupted `recipient` field of `Command::UnlockNativeToken`/`Command::MintForeignToken` to be `0x00...00`, which — once relayed and executed on Ethereum — either burns unlocked ERC20/native ETH or mints a wrapped token to the null address, both of which are unrecoverable within this repo's scope of control (the token conservation invariant for the transferred `amount` is violated for the user's chosen beneficiary). This is a pure protocol-logic defect reachable by any unprivileged XCM sender.

## Likelihood Explanation
High likelihood. The only requirement is for an unprivileged user to send a standard XCM asset-transfer program (e.g., via `pallet_xcm::transfer_assets` from AssetHub or any parachain routed through `EthereumBlobExporter`) with a crafted `beneficiary: AccountKey20 { key: [0u8;20], network: None }`. No governance, validator, or relayer privilege is needed, and the message passes all existing checks (`network_matches`, asset filter matching, non-zero amount) before being queued and committed for relay.

## Recommendation
Add an explicit guard immediately after extracting `recipient` in `make_unlock_native_token_command`/`make_mint_foreign_token_command` (v1, `mod.rs:253-260` and the analogous block in `make_mint_foreign_token_command`) and in `convert` (v2, `convert.rs:265-272`): `ensure!(recipient != H160::zero(), XcmConverterError::InvalidBeneficiary)` (adding a new `InvalidBeneficiary`/reusing `BeneficiaryResolutionFailed` error variant), causing the XCM conversion to fail (`SendError::Unroutable`) rather than producing a `Command` addressed to `0x0`.

## Proof of Concept
1. From AssetHub (or any source parachain), submit an XCM program via `pallet_xcm::send`/`transfer_assets` targeting Ethereum, ending with:
```
DepositAsset {
    assets: Wild(All),
    beneficiary: AccountKey20 { network: None, key: [0u8; 20] }.into(),
}
```
2. `EthereumBlobExporter::validate` calls `XcmConverter::convert`, which via `make_unlock_native_token_command` (v1, lines 253-260) or `convert` (v2, lines 265-272) successfully resolves `recipient = H160([0;20])` without error.
3. `Command::UnlockNativeToken { recipient: H160::zero(), .. }` (or `MintForeignToken`) is returned, validated, and queued via `OutboundQueue::validate`/`deliver`, becoming committed into the outbound message merkle root for relay.
4. A unit test can assert that `XcmConverter::convert` on this program returns `Ok((Command::UnlockNativeToken { recipient: H160::zero(), .. }, _))` instead of an `Err`, demonstrating the missing validation.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L253-260)
```rust
		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L313-316)
```rust
		Ok((
			Command::UnlockNativeToken { agent_id: self.agent_id, token, recipient, amount },
			*topic_id,
		))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L151-151)
```rust
			commands.push(Command::UnlockNativeToken { token, recipient, amount });
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L183-183)
```rust
			commands.push(Command::MintForeignToken { token_id, recipient, amount });
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L265-272)
```rust
		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L100-130)
```rust
	/// Transfer ERC20 tokens
	UnlockNativeToken {
		/// ID of the agent
		agent_id: H256,
		/// Address of the ERC20 token
		token: H160,
		/// The recipient of the tokens
		recipient: H160,
		/// The amount of tokens to transfer
		amount: u128,
	},
	/// Register foreign token from Polkadot
	RegisterForeignToken {
		/// ID for the token
		token_id: H256,
		/// Name of the token
		name: Vec<u8>,
		/// Short symbol for the token
		symbol: Vec<u8>,
		/// Number of decimal places
		decimals: u8,
	},
	/// Mint foreign token from Polkadot
	MintForeignToken {
		/// ID for the token
		token_id: H256,
		/// The recipient of the newly minted tokens
		recipient: H160,
		/// The amount of tokens to mint
		amount: u128,
	},
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L153-181)
```rust
	/// Unlock ERC20 tokens
	UnlockNativeToken {
		/// Address of the ERC20 token
		token: H160,
		/// The recipient of the tokens
		recipient: H160,
		/// The amount of tokens to transfer
		amount: u128,
	},
	/// Register foreign token from Polkadot
	RegisterForeignToken {
		/// ID for the token
		token_id: H256,
		/// Name of the token
		name: Vec<u8>,
		/// Short symbol for the token
		symbol: Vec<u8>,
		/// Number of decimal places
		decimals: u8,
	},
	/// Mint foreign token from Polkadot
	MintForeignToken {
		/// ID for the token
		token_id: H256,
		/// The recipient of the newly minted tokens
		recipient: H160,
		/// The amount of tokens to mint
		amount: u128,
	},
```
