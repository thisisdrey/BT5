No vulnerability found for this question.

The reported issue (CL-2021-28) concerns the intrinsic bit-security level of the BLS12-381 pairing curve used in Ethereum's consensus-layer BLS signature scheme — a parameter-choice/theoretical cryptographic concern, not an exploitable code defect. The only BLS12-381 usage in the in-scope repository is in the CKD (conditional key derivation) module [1](#0-0) , which performs pairing checks against MPC-supplied public keys for key-derivation verification — it is unrelated to `MultiPayload` signature verification, nonce/settlement logic, or balance/asset conservation in `contracts/defuse/**`. Intent authorization in this codebase instead relies on Ed25519, secp256k1/ECDSA, P-256 (WebAuthn) via `SignedPayload::verify` implementations across the `MultiPayload` variants [2](#0-1) , none of which use BLS. There is no reachable path in `contracts/defuse/**`, `contracts/wallet/**`, `contracts/poa/**`, or the other in-scope directories where a BLS curve security-level concern would break an authorisation, replay, conservation, settlement, identity, or fee boundary as required by the validation rules, and curve-parameter security-level findings are explicitly excluded as theoretical/best-practice notes.

### Citations

**File:** crates/mpc/ckd/src/lib.rs (L211-249)
```rust
    /// Check that `e(sig, g2) = e(hash_point, mpc_public_key)`
    ///
    /// See <https://github.com/near/mpc/blob/f7a959d2bfd723e92c3bd71a5b60e03d972a2ddb/crates/ckd-example-cli/src/ckd.rs#L100-L115>
    #[must_use = "check whether verification succeeded"]
    fn verify(mpc_public_key: G2Affine, app_id: &[u8; 32], signature: G1Affine) -> bool {
        if !is_valid_g1(&signature) || !is_valid_g2(&mpc_public_key) {
            return false;
        }

        let minus_g2 = -G2Affine::generator();
        let hp = hash_point(&mpc_public_key, app_id);

        cfg_select! {
            near => {
                ::near_sdk::env::bls12381_pairing_check(
                    [
                        signature.to_uncompressed().as_slice(),
                        minus_g2.to_uncompressed().as_slice(),

                        hp.to_uncompressed().as_slice(),
                        mpc_public_key.to_uncompressed().as_slice(),
                    ]
                    .concat(),
                )
            }
            _ => {
                use blstrs::Bls12;
                use pairing::{MillerLoopResult, MultiMillerLoop, group::Group};

                Bls12::multi_miller_loop(&[
                    (&signature, &minus_g2.into()),
                    (&hp.into(), &mpc_public_key.into()),
                ])
                .final_exponentiation()
                .is_identity()
                .into()
            }
        }
    }
```

**File:** contracts/defuse/core/src/payload/multi.rs (L80-95)
```rust
impl SignedPayload for MultiPayload {
    type PublicKey = PublicKey;

    #[inline]
    fn verify(&self) -> Option<Self::PublicKey> {
        match self {
            Self::Nep413(payload) => payload.verify().map(PublicKey::Ed25519),
            Self::Erc191(payload) => payload.verify().map(PublicKey::Secp256k1),
            Self::Tip191(payload) => payload.verify().map(PublicKey::Secp256k1),
            Self::RawEd25519(payload) => payload.verify().map(PublicKey::Ed25519),
            Self::WebAuthn(payload) => payload.verify(),
            Self::TonConnect(payload) => payload.verify().map(PublicKey::Ed25519),
            Self::Sep53(payload) => payload.verify().map(PublicKey::Ed25519),
        }
    }
}
```
