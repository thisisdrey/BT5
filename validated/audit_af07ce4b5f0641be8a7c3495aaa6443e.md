### Title
Signed `MultiPayload`/`DefusePayload` intents lack a chain identifier, permitting cross-chain replay of a single authorization after a NEAR network split - (File: contracts/defuse/core/src/engine/mod.rs)

### Summary
### Finding Description
The Defuse intents engine authorizes a signed `MultiPayload` by verifying the cryptographic signature and then binding it to state solely via `signer_id`, `verifying_contract` (the contract's own `AccountId`), `deadline`, and `nonce`: [1](#0-0) 

`verifying_contract` is simply `env::current_account_id()` of the deployed contract, with no chain-specific salt: [2](#0-1) 

The underlying `Nep413Payload` that is actually hashed and signed contains only `message`, `nonce`, `recipient`, `callback_url` — no chain identifier at all: [3](#0-2) 

Compare this with the sibling wallet-contract signing scheme (`contracts/wallet`), which explicitly embeds and checks a `chain_id` field against `env::chain_id()` before honoring a signed request: [4](#0-3) [5](#0-4) 

The Defuse intents payload path (`contracts/defuse/core/src/payload/*`, `contracts/defuse/core/src/engine/mod.rs`) has no equivalent binding. Nonce consumption (`commit_nonce`) is local per-account contract storage: [6](#0-5) 

This is the same bug class as the referenced external report (`IncentivizedMockImplementation` hardcoded `SOURCE_IDENTIFIER` causing cross-chain signature replay after a hardfork): a signature-verification input set that omits an unambiguous chain/network identifier, so once a chain split occurs (two independently-progressing state tries sharing identical pre-fork history and identical contract account IDs), a signature valid on one branch remains valid — and unconsumed — on the other branch.

### Impact Explanation
If the NEAR network undergoes a chain split/hardfork, both resulting chains will (at the fork point) share identical `Contract` state, including empty/unused nonce sets for all accounts, and identical `verifying_contract` account IDs. A user's single signed `MultiPayload` — authorizing e.g. a withdrawal, transfer, or `token_diff` swap — can be submitted and executed on chain A (consuming the nonce there), and the exact same signed payload (identical signature, identical nonce, identical `verifying_contract`) can independently be submitted and executed on chain B, since chain B's nonce/account state is a separate, disjoint storage trie that never observes chain A's nonce commitment. This breaks the "one signed `MultiPayload` settles once" invariant and results in the same authorized balance change (withdrawal, transfer, swap) being executed twice for the cost of one signature — a form of unauthorized double execution/duplication of value, matching the Critical impact category "one signed payload settling more than once."

### Likelihood Explanation
This requires an actual NEAR network chain-split/hardfork event to occur, which is rare and, per the original report, "unusual" and dependent on future protocol changes — hence Medium likelihood consistent with the original disclosure's own severity rating. It requires no privileged access, no relayer key, and no victim cooperation beyond the pre-existing signed payload; any unprivileged holder of a validly signed `MultiPayload` (including the original signer or an observer who intercepts a broadcast transaction/payload) can replay it on the sibling chain.

### Recommendation
Bind the signed payload's `verifying_contract` (or `DefusePayload`) to a chain-specific identifier by concatenating/prefixing `env::current_account_id()` with the equivalent of NEAR's chain ID / genesis hash — mirroring the approach already used in `contracts/wallet/src/message.rs`'s `RequestMessage.chain_id` check against `env::chain_id()` — and reject signed intents whose embedded identifier does not match the executing chain's identifier at verification time in `Engine::execute_signed_intent`.

### Proof of Concept
1. Assume NEAR network splits into chain A and chain B at block N, with identical state (identical `defuse` contract deployment, identical account balances/nonces).
2. A user signs one `MultiPayload` (e.g. `Nep413` standard) authorizing `token_diff`/`ft_withdraw`, with `verifying_contract = "intents.near"`, some `nonce`, and `deadline` in the future.
3. Attacker/relayer submits the payload to `intents.near` on chain A via `execute_signed_intents` → `execute_signed_intent` (contracts/defuse/core/src/engine/mod.rs:42-83); signature verifies, `verifying_contract` matches, nonce is unused, intent executes and nonce is committed only in chain A's storage.
4. Attacker/relayer submits the byte-identical `MultiPayload` to `intents.near` on chain B. Because chain B's contract storage is an independently-evolving copy that never received chain A's nonce commitment, `has_public_key`/`is_nonce_used` checks pass again, and the same intent (e.g., withdrawal) executes a second time, effectively duplicating the value moved from a single signature.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L42-77)
```rust
    fn execute_signed_intent(&mut self, signed: MultiPayload) -> Result<()> {
        // verify signed payload and get public key
        let public_key = signed.verify().ok_or(DefuseError::InvalidSignature)?;

        // calculate intent hash
        let hash = signed.hash();

        // extract NEP-413 payload
        let DefusePayload::<DefuseIntents> {
            signer_id,
            verifying_contract,
            deadline,
            nonce,
            message: intents,
        } = signed.extract_defuse_payload()?;

        // check recipient
        if verifying_contract != *self.state.verifying_contract() {
            return Err(DefuseError::WrongVerifyingContract);
        }

        self.inspector.on_deadline(deadline);

        // make sure message is still valid
        if deadline < Timestamp::now() {
            return Err(DefuseError::DeadlineExpired);
        }

        // make sure the account has this public key
        if !self.state.has_public_key(&signer_id, &public_key) {
            return Err(DefuseError::PublicKeyNotExist(signer_id, public_key));
        }

        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;
```

**File:** contracts/defuse/src/contract/intents/state.rs (L20-24)
```rust
impl StateView for Contract {
    #[inline]
    fn verifying_contract(&self) -> Cow<'_, AccountIdRef> {
        Cow::Owned(env::current_account_id())
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L123-130)
```rust
    #[inline]
    fn commit_nonce(&mut self, account_id: AccountId, nonce: Nonce) -> Result<()> {
        self.accounts
            .get_or_create(account_id.clone())
            .get_mut()
            .ok_or(DefuseError::AccountLocked(account_id))?
            .commit_nonce(nonce)
    }
```

**File:** crates/signatures/nep413/src/lib.rs (L69-82)
```rust
pub struct Nep413Payload {
    pub message: String,

    #[cfg_attr(feature = "serde", serde_as(as = "::serde_with::base64::Base64"))]
    pub nonce: [u8; 32],

    pub recipient: String,

    #[cfg_attr(
        feature = "serde",
        serde(default, skip_serializing_if = "Option::is_none")
    )]
    pub callback_url: Option<String>,
}
```

**File:** contracts/wallet/src/contract.rs (L358-387)
```rust
        Ok(match input {
            WalletAuthorization::Signature { msg, proof } => {
                if !self.0.is_signature_allowed() {
                    return Err(Error::SignatureDisabled);
                }

                // check chain_id
                if msg.chain_id != env::chain_id() {
                    return Err(Error::InvalidChainId);
                }

                // check signer_id
                if msg.signer_id != env::current_account_id() {
                    return Err(Error::InvalidSignerId(msg.signer_id));
                }

                // check path
                if msg.path != path {
                    return Err(Error::InvalidPath);
                }

                // check timestamp
                if Timestamp::now() < msg.timestamp {
                    return Err(Error::FromTheFuture);
                }

                // verify signature
                if !S::verify_offchain_msg(&self.0.public_key, &msg, &proof) {
                    return Err(Error::InvalidSignature);
                }
```

**File:** contracts/wallet/src/message.rs (L147-188)
```rust
impl RequestMessage {
    /// A prefix used for [canonical hash](Self::hash).
    pub const DOMAIN_SEPARATOR: &[u8] = b"NEAR_WALLET_CONTRACT/V1";

    /// Returns canonical hash of the request message:
    ///
    /// ```text
    /// SHA3-256(b"NEAR_WALLET_CONTRACT/V1" || borsh(msg))
    /// ```
    ///
    /// # Examples
    ///
    /// ```rust
    /// # use core::time::Duration;
    /// # use defuse_wallet::{Request, RequestMessage, Timestamp};
    /// # use hex_literal::hex;
    /// let msg = RequestMessage {
    ///     pay_for_gas: false,
    ///     chain_id: "mainnet".to_string(),
    ///     signer_id: "0s0000000000000000000000000000000000000000".parse().unwrap(),
    ///     nonce: 0,
    ///     created_at: Timestamp::UNIX_EPOCH,
    ///     timeout: Duration::from_secs(3600),
    ///     request: Request::new(),
    /// };
    ///
    /// assert_eq!(
    ///     msg.hash(),
    ///     hex!("a8df85c0e8793716904fd57e9bef7d83b12773508bd3c9fca554afbfb108d4b5"),
    /// );
    /// ```
    #[cfg(all(feature = "digest", feature = "borsh"))]
    pub fn hash(&self) -> [u8; 32] {
        use defuse_digest::{Digest, sha3::Sha3_256};
        use digest_io::IoWrapper;

        let mut hasher = IoWrapper(Sha3_256::new_with_prefix(Self::DOMAIN_SEPARATOR));
        // serialize directly to hasher
        ::borsh::to_writer(&mut hasher, self).expect("borsh: failed to serialize");

        hasher.0.finalize().into()
    }
```
