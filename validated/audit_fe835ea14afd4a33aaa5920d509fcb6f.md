The digest binds the entire message_bits (relayers + payload type + payload) via SHA512_256 preimage resistance, not just a "length window" — there is no partial/truncated hashing that would allow the collision the question describes.

### Title
No vulnerability found - Preamble signature is bound to full message_bits via cryptographic hash, no splicing possible - (File: stackslib/src/net/codec.rs)

### Summary
The question hypothesizes that an attacker could take a valid `Preamble.signature` from one `StacksMessage` and splice it onto a different payload if the two payloads' `message_bits` happened to collide in a "digest length window." Tracing `Preamble::sign`/`Preamble::verify` shows the digest is a full `Sha512_256` hash over the preamble bytes (with blank signature) concatenated with the *entire* `message_bits` (relayers + payload type + payload), and verification recomputes this same full-length hash from the attacker-supplied bytes before checking the ECDSA signature. There is no truncation, no fixed "window," and no omission of the payload type byte.

### Finding Description
`Preamble::sign` computes `sha2.update(&preamble_bits); sha2.update(message_bits); digest_bits = sha2.finalize()` then signs `digest_bits` with the peer's private key [1](#0-0) . `Preamble::verify` performs the identical hash computation over the preamble (with signature blanked) plus the caller-supplied `message_bits`, then calls `pubkey.verify(&digest_bits, &self.signature)` [2](#0-1) . Callers construct `message_bits` by serializing `relayers` then `payload` in full — `self.relayers.consensus_serialize(&mut message_bits); self.payload.consensus_serialize(&mut message_bits);` — both in `StacksMessage::do_sign` and in `verify_secp256k1`/`verify_payload_bytes` [3](#0-2) [4](#0-3) [5](#0-4) . Because `payload.consensus_serialize` for `StacksMessageType` writes the type-ID byte followed by the full payload encoding (as seen in the enum's codec dispatch, e.g. `StacksMessageID::Ping/Pong/...`), the type byte and every payload field are part of the hashed preimage, not excluded from it. There is no "length window" concept in this code — `sha2.update` consumes the entire `message_bits` slice regardless of its length, and SHA512_256 is a full cryptographic hash with no meaningful collision-finding capability for an unprivileged remote attacker. Splicing a signature from a `Ping` message onto a `Pong` (or any other) payload would require finding a second preimage/collision for SHA512_256, which is computationally infeasible — not a code defect. The premise of a "digest length window" that could be satisfied by attacker-chosen bytes does not exist in this implementation.

### Impact Explanation
No impact: the guard (full-message SHA512_256 + ECDSA verification in `Preamble::verify`) is intact and there is no path where a different payload produces a colliding digest under realistic attacker capability. No forged message can be accepted from a legitimate peer's identity via this vector.

### Likelihood Explanation
Not applicable — the described attack requires breaking SHA512_256 collision/second-preimage resistance, which is outside any remote unprivileged attacker's practical capability and is not a code-level defect in this repository.

### Recommendation
None needed; the current design (hash-then-sign over preamble + relayers + type + payload) already correctly binds the signature to the exact message contents.

### Proof of Concept
The existing test `codec_sign_and_verify` in `stackslib/src/net/codec.rs` already demonstrates the intended binding (sign a `Ping`, verify against the same payload succeeds) [6](#0-5) . A confirmatory test would sign a `Ping` message, then call `Preamble::verify` (or `verify_secp256k1`) with the resulting signature but `message_bits` built from a different payload (e.g., a `Pong`), asserting the call returns `Err(net_error::VerifyingError(..))` — this is expected to pass today, confirming no gap exists.

### Citations

**File:** stackslib/src/net/codec.rs (L82-103)
```rust
        let mut digest_bits = [0u8; 32];
        let mut sha2 = Sha512_256::new();

        // serialize the premable with a blank signature
        let old_signature = self.signature.clone();
        self.signature = MessageSignature::empty();

        let mut preamble_bits = vec![];
        self.consensus_serialize(&mut preamble_bits)?;
        self.signature = old_signature;

        sha2.update(&preamble_bits[..]);
        sha2.update(message_bits);

        digest_bits.copy_from_slice(sha2.finalize().as_slice());

        let sig = privkey
            .sign(&digest_bits)
            .map_err(|se| net_error::SigningError(se.to_string()))?;

        self.signature = sig;
        Ok(())
```

**File:** stackslib/src/net/codec.rs (L108-140)
```rust
    pub fn verify(
        &mut self,
        message_bits: &[u8],
        pubkey: &Secp256k1PublicKey,
    ) -> Result<(), net_error> {
        let mut digest_bits = [0u8; 32];
        let mut sha2 = Sha512_256::new();

        // serialize the preamble with a blank signature
        let sig_bits = self.signature.clone();
        self.signature = MessageSignature::empty();

        let mut preamble_bits = vec![];
        self.consensus_serialize(&mut preamble_bits)?;
        self.signature = sig_bits;

        sha2.update(&preamble_bits[..]);
        sha2.update(message_bits);

        digest_bits.copy_from_slice(sha2.finalize().as_slice());

        let res = pubkey
            .verify(&digest_bits, &self.signature)
            .map_err(|_ve| net_error::VerifyingError("Failed to verify signature".to_string()))?;

        if res {
            Ok(())
        } else {
            Err(net_error::VerifyingError(
                "Invalid message signature".to_string(),
            ))
        }
    }
```

**File:** stackslib/src/net/codec.rs (L1422-1428)
```rust
    fn do_sign(&mut self, private_key: &Secp256k1PrivateKey) -> Result<(), net_error> {
        let mut message_bits = vec![];
        self.relayers.consensus_serialize(&mut message_bits)?;
        self.payload.consensus_serialize(&mut message_bits)?;

        self.preamble.payload_len = message_bits.len() as u32;
        self.preamble.sign(&message_bits[..], private_key)
```

**File:** stackslib/src/net/codec.rs (L1496-1502)
```rust
        let mut message_bits = vec![];
        self.relayers.consensus_serialize(&mut message_bits)?;
        self.payload.consensus_serialize(&mut message_bits)?;

        let mut p = self.preamble.clone();
        p.verify(&message_bits, &secp256k1_pubkey).map(|_m| ())
    }
```

**File:** stackslib/src/net/codec.rs (L1577-1587)
```rust
    fn verify_payload_bytes(
        &mut self,
        key: &StacksPublicKey,
        preamble: &Preamble,
        bytes: &[u8],
    ) -> Result<(), Error> {
        let preamble_bytes = bytes.get(..preamble.payload_len as usize).ok_or_else(|| {
            Error::UnderflowError("Not enough bytes to form a StacksMessage".to_string())
        })?;
        preamble.clone().verify(preamble_bytes, key).map(|_m| ())
    }
```

**File:** stackslib/src/net/codec.rs (L2724-2742)
```rust
    #[test]
    fn codec_sign_and_verify() {
        let privkey = Secp256k1PrivateKey::random();
        let pubkey_buf =
            StacksPublicKeyBuffer::from_public_key(&Secp256k1PublicKey::from_private(&privkey));

        let mut ping = StacksMessage::new(
            PEER_VERSION_TESTNET,
            0x9abcdef0,
            12345,
            &BurnchainHeaderHash([0x11; 32]),
            12339,
            &BurnchainHeaderHash([0x22; 32]),
            StacksMessageType::Ping(PingData { nonce: 0x01020304 }),
        );

        ping.sign(444, &privkey).unwrap();
        ping.verify_secp256k1(&pubkey_buf).unwrap();
    }
```
