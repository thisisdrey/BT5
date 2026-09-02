### Title
Signed intents (`MultiPayload`/`DefusePayload`) omit chain identifier, enabling cross-fork signature replay - (File: `contracts/defuse/core/src/payload/mod.rs`, `contracts/defuse/src/contract/intents/state.rs`)

### Summary
The defuse Verifier's signed-intent schema (`DefusePayload`, wrapped by every `MultiPayload` variant — NEP-413, ERC-191, TIP-191, TonConnect, SEP-53, WebAuthn, raw Ed25519) binds a signature only to `signer_id`, `verifying_contract` (an `AccountId`), `deadline`, and `nonce`. It contains no chain/network identifier. `verifying_contract` is populated from `env::current_account_id()`, which is just an account-name string and is identical across any fork of the same NEAR network (or across a redeployment of the same contract account name on a different NEAR-compatible chain/testnet-clone). This is the exact bug class described in the external report (EIP-2612 domain separator lacking `chainId` validation against `block.chainid`), applied to the NEP-413/`MultiPayload` signing domain instead of EIP-712.

### Finding Description
`execute_signed_intent` in the engine validates a signed intent purely against `verifying_contract`, `deadline`, and `nonce`: [1](#0-0) 

`DefusePayload` — the struct that every `MultiPayload` variant extracts and that is actually covered by the signature — has no `chain_id` field: [2](#0-1) 

The value it compares against, `verifying_contract`, is derived solely from the current account id, with no chain/network binding: [3](#0-2) 

By contrast, the sibling `wallet` contract in this same repo explicitly recognizes this exact risk and defends against it by checking `msg.chain_id != env::chain_id()` before accepting a signed request: [4](#0-3) 

No equivalent check exists anywhere under `contracts/defuse/**` (confirmed no `chain_id` references in that directory tree). This means if the same NEAR account namespace (e.g., `intents.near`) is deployed/re-instantiated on more than one chain that share account ids — whether via a state-preserving hard fork of NEAR itself, or a duplicate/cloned deployment of the identical Verifier contract state and account id on a distinct network — a previously-signed `MultiPayload` (which only pins `signer_id` + `verifying_contract` + `nonce` + `deadline`) remains valid and can be submitted to `execute_intents` on the second chain, moving/crediting the signer's balance there without a fresh authorization.

### Impact Explanation
This breaks the authorisation binding "one signed `MultiPayload` == one authorised settlement on one specific chain." An unprivileged relayer/attacker holding a previously-broadcast valid `MultiPayload` (these are routinely passed to public, permissionless relayers per the intents design) could replay it on a second chain instance sharing the signer's account id and the verifier's account id, executing token transfers/diffs/withdrawals a second time outside the intended chain — a form of cross-domain settlement replay. This matches the Critical impact criterion "one signed payload settling more than once."

### Likelihood Explanation
Likelihood is bounded by the practical rarity of an actual chain fork or duplicate-namespace deployment of the exact Verifier contract with matching account ids and un-reused nonces/salts; it is a low-frequency but structurally real event class (this is literally the scenario the referenced Trail-of-Bits finding targets for EIP-2612, and the codebase already treats it as real enough to fix in `contracts/wallet`). The lack of any mitigation specifically in `contracts/defuse` (in contrast to `contracts/wallet`) indicates an inconsistent application of the same threat model within one repository.

### Recommendation
Add a chain/network identifier field to `DefusePayload` (or to the outer signed envelope) and validate it against `env::chain_id()` (or equivalent) in `execute_signed_intent`, mirroring the check already implemented in `contracts/wallet/src/contract.rs`. As a short-term mitigation, recompute/verify this binding dynamically rather than baking a static value into a cached domain separator, and document explicitly the assumption that `verifying_contract` alone is not chain-unique.

### Proof of Concept
1. Verifier contract account `intents.near` operates on Chain A; a signer signs a `MultiPayload` (e.g., NEP-413) authorizing a `TokenDiff`/`FtWithdraw` intent, with `verifying_contract = "intents.near"`, valid `nonce`/`deadline`.
2. Chain A undergoes a fork retaining the same state/account namespace (Chain A and Chain A′ both have `intents.near` with identical nonce/salt state at fork time), or the same Verifier account id and state are otherwise duplicated onto a second chain.
3. The relayer submits the same signed `MultiPayload` to `execute_intents` on Chain A′. Because `execute_signed_intent` only checks `verifying_contract` (matches, since it's just `"intents.near"`), `deadline` (still valid), and unused `nonce` (unused on the forked chain if the fork point precedes commitment or if fork/duplication resets nonce state), the intent executes successfully on Chain A′ — settling the same signed authorization twice across two distinct chains. [5](#0-4)

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

**File:** contracts/defuse/core/src/payload/mod.rs (L24-34)
```rust
pub struct DefusePayload<T> {
    pub signer_id: AccountId,
    pub verifying_contract: AccountId,
    pub deadline: Timestamp,
    #[serde_as(as = "Base64")]
    #[cfg_attr(feature = "schemars-v0_8", schemars(example = "self::examples::nonce"))]
    pub nonce: Nonce,

    #[serde(flatten)]
    pub message: T,
}
```

**File:** contracts/defuse/src/contract/intents/state.rs (L20-24)
```rust
impl StateView for Contract {
    #[inline]
    fn verifying_contract(&self) -> Cow<'_, AccountIdRef> {
        Cow::Owned(env::current_account_id())
    }
```

**File:** contracts/wallet/src/contract.rs (L182-185)
```rust
        // check chain_id
        if msg.chain_id != env::chain_id() {
            return Err(Error::InvalidChainId);
        }
```
