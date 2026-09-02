### No vulnerability found for this question.

The premise that `domain`/`timestamp` are unchecked outside the deadline comparison is incorrect: `TonConnectPayload::try_prehash` folds `address`, `domain`, `timestamp`, and the payload bytes (`text`) into the SHA-256 preimage that is signed [1](#0-0) , and `SignedTonConnectPayload::hash`/`verify` operate over that same full `TonConnectPayload` via `try_prehash`/`TonConnect::verify` [2](#0-1) . Any change to `domain` (or `timestamp`) therefore changes the signed hash, so `verify()` fails Ed25519 verification unless the attacker also has a fresh valid signature from the legitimate signer's key for the new `domain` — which is exactly the "victim private key" precondition explicitly excluded by the rules (attacker only controls their own keys). The `p.deadline < self.timestamp` check in `extract_defuse_payload` is an additional sanity/replay-window check on top of this cryptographic binding, not the sole binding between the outer envelope and inner `text` [3](#0-2) . Since `domain` is cryptographically bound to the signature, an attacker cannot take a validly-signed payload for one `domain` and resubmit it with a different `domain` while keeping the same signature valid — there is no forgery path here without possessing the signer's private key, which is out of scope.

### Citations

**File:** crates/signatures/ton-connect/src/lib.rs (L70-107)
```rust
impl TonConnectPayload {
    pub fn try_prehash(&self) -> Option<[u8; 32]> {
        let timestamp: u64 = self.timestamp.as_secs().try_into().ok()?;

        let (prefix, payload) = match &self.payload {
            TonConnectPayloadSchema::Text { text } => (b"txt", text.as_bytes()),
            TonConnectPayloadSchema::Binary { bytes } => (b"bin", bytes.as_slice()),
            #[cfg(feature = "cell")]
            TonConnectPayloadSchema::Cell { schema_crc, cell } => {
                return self::cell::TonConnectCellMessage {
                    schema_crc: *schema_crc,
                    timestamp,
                    user_address: &self.address,
                    app_domain: &self.domain,
                    payload: &cell,
                }
                .hash();
            }
        };

        let domain_len: u32 = self.domain.len().try_into().ok()?;
        let payload_len: u32 = payload.len().try_into().ok()?;

        // 0xffff ++ "ton-connect/sign-data/" ++ Address ++ AppDomain ++ Timestamp ++ Payload
        let prehash = Sha256::new_with_prefix(b"\xFF\xFFton-connect/sign-data/")
            .chain_update(self.address.workchain_id.to_be_bytes())
            .chain_update(self.address.address)
            .chain_update(domain_len.to_be_bytes())
            .chain_update(self.domain.as_bytes())
            .chain_update(timestamp.to_be_bytes())
            .chain_update(prefix)
            .chain_update(payload_len.to_be_bytes())
            .chain_update(payload)
            .finalize()
            .into();

        Some(prehash)
    }
```

**File:** contracts/defuse/core/src/payload/ton_connect.rs (L23-42)
```rust
impl Payload for SignedTonConnectPayload {
    #[inline]
    fn hash(&self) -> [u8; 32] {
        self.payload.try_prehash().expect("ton-connect hash")
    }
}

impl SignedPayload for SignedTonConnectPayload {
    type PublicKey = Ed25519PublicKey;

    #[inline]
    fn verify(&self) -> Option<Self::PublicKey> {
        TonConnect::verify(
            &self.public_key.try_into().ok()?,
            &self.payload,
            &self.signature.into(),
        )
        .then_some(&self.public_key)
        .copied()
    }
```

**File:** contracts/defuse/core/src/payload/ton_connect.rs (L63-84)
```rust
    fn extract_defuse_payload(self) -> Result<DefusePayload<T>, Self::Error> {
        let TonConnectPayloadSchema::Text { text } = self.payload else {
            return Err(Error::custom("only text payload supported"));
        };

        let p: DefusePayload<T> = serde_json::from_str(&text)?;

        // TON Connect [specification](https://docs.tonconsole.com/academy/sign-data#in-a-smart-contract-on-chain)
        // requires to check that "timestamp is recent". We don't have fixed TTL
        // for off-chain signatures but rather check if `deadline` is not expired.
        //
        // At first, we were asserting `(timestamp <= now())`, but that  was causing
        // `simulate_intents()` to fail, since sometimes signed intent is simulated
        // right after signing.
        //
        // So, we ended up to assert at least following:
        if p.deadline < self.timestamp {
            return Err(Error::custom("deadline < timestamp"));
        }

        Ok(p)
    }
```
