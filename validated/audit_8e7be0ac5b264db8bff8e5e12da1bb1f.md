The code at [1](#0-0) . This is exactly the claimed correct binding: `has_public_key(payload.signer_id, recovered_pk)`, not `has_public_key(attacker, recovered_pk)`.

Critically, `signer_id` is not a separate unsigned field supplied by the caller — it is embedded **inside** the signed payload content that produced the signature/hash:

- For NEP-413: `signer_id` is part of `Nep413DefuseMessage`, which is serialized into `self.message` and covered by `Nep413::verify` / the payload hash. [2](#0-1) 
- For ERC-191, TIP-191, RawEd25519, Sep53, WebAuthn: `signer_id` comes from `DefusePayload` deserialized out of the same `payload` string whose hash (`Erc191::prehash`, `Tip191::prehash`, `Sha256::digest`, etc.) is exactly what gets signature-verified. [3](#0-2) [4](#0-3) 

So an attacker cannot forge a payload with `signer_id = victim` while keeping their own signature valid, because changing `signer_id` changes the hashed content and invalidates the ERC-191/TIP-191/NEP-413/etc. signature verification. There is no code path where the engine takes an attacker-supplied `signer_id` disconnected from the signed content and only checks the attacker's own `has_public_key(attacker, pk)`. The binding described in the question is already correctly implemented at [5](#0-4) : `has_public_key(&signer_id, &public_key)` uses the `signer_id` extracted from the very payload whose signature was verified, so `(signer_id, public_key)` authorized == `(signer_id, public_key)` that signed by construction.

The webauthn test even demonstrates the derived binding explicitly: `dp.signer_id` equals the implicit account derived from the public key that produced the valid signature. [6](#0-5) 

#No vulnerability found for this question.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L42-73)
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
```

**File:** contracts/defuse/core/src/payload/nep413.rs (L22-53)
```rust
pub struct Nep413DefuseMessage<T> {
    pub signer_id: AccountId,

    pub deadline: Timestamp,

    #[serde(flatten)]
    pub message: T,
}

impl<T> ExtractDefusePayload<T> for Nep413Payload
where
    T: DeserializeOwned,
{
    type Error = serde_json::Error;

    fn extract_defuse_payload(self) -> Result<DefusePayload<T>, Self::Error> {
        let Nep413DefuseMessage {
            signer_id,
            deadline,
            message,
        } = serde_json::from_str(&self.message)?;

        Ok(DefusePayload {
            signer_id,
            verifying_contract: self.recipient.parse().map_err(|_| {
                de::Error::invalid_value(de::Unexpected::Str(&self.recipient), &"AccountId")
            })?,
            deadline,
            nonce: self.nonce,
            message,
        })
    }
```

**File:** contracts/defuse/core/src/payload/erc191.rs (L20-47)
```rust
impl Payload for SignedErc191Payload {
    #[inline]
    fn hash(&self) -> [u8; 32] {
        Erc191::prehash(&self.payload)
    }
}

impl SignedPayload for SignedErc191Payload {
    type PublicKey = Secp256k1UncompressedPublicKey;

    #[inline]
    fn verify(&self) -> Option<Self::PublicKey> {
        let (signature, recovery_id) = self.signature.try_into().ok()?;

        Erc191::recover(&self.payload, &signature, recovery_id).map(Into::into)
    }
}

impl<T> ExtractDefusePayload<T> for SignedErc191Payload
where
    T: DeserializeOwned,
{
    type Error = serde_json::Error;

    #[inline]
    fn extract_defuse_payload(self) -> Result<DefusePayload<T>, Self::Error> {
        serde_json::from_str(&self.payload)
    }
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

**File:** contracts/defuse/core/src/payload/webauthn.rs (L100-112)
```rust
        let public_key = p.verify().expect("invalid signature");
        assert_eq!(
            public_key,
            "p256:2V8Np9vGqLiwVZ8qmMmpkxU7CTRqje4WtwFeLimSwuuyF1rddQK5fELiMgxUnYbVjbZHCNnGc6fAe4JeDcVxgj3Q"
                .parse()
                .unwrap(),
        );
        assert_eq!(public_key.to_implicit_account_id(), SIGNER_ID);

        let dp: DefusePayload<DefuseIntents> = p.extract_defuse_payload().unwrap();
        dbg!(&dp);
        assert_eq!(dp.signer_id, SIGNER_ID);
    }
```
