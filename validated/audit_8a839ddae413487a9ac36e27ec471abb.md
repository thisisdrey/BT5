#No Vulnerability found for this question.

Analysis: The `PublicKeyHandle` encoding used by `append_key_handle_trie_id` (delegating to borsh serialization) uses disjoint discriminant tags per key type — `0` for ED25519, `1` for SECP256K1, `3` for the ML-DSA-65 hash form [1](#0-0) , so a raw ED25519/SECP256K1 key can never collide with an ML-DSA-65 hash entry — the leading tag byte alone differentiates them, as documented explicitly in the design doc [2](#0-1) .

Within the same tag, ED25519/SECP256K1 entries store the full raw public key bytes (32/64 bytes) verbatim [3](#0-2) , so two distinct `PublicKey` values of the same type trivially cannot produce identical `key_data` — equal bytes would mean it's literally the same key, not two distinct keys.

The only remaining collision surface is two *distinct* ML-DSA-65 public keys hashing to the same 32-byte SHA3-256 digest under a fixed domain tag `b"near:ml-dsa-65-pubkey-hash:v1"` [4](#0-3) . That would require breaking SHA3-256 collision resistance (finding a full 256-bit collision, ~2^128 work), which is outside any practical, reachable-by-an-unprivileged-attacker scenario and is a fundamental cryptographic hardness assumption rather than a code defect. This is exactly the kind of speculative, non-reachable claim the audit rules direct to reject.

### Citations

**File:** core/crypto/src/signature.rs (L552-558)
```rust
    fn key_tag(&self) -> KeyTag {
        match self {
            PublicKeyHandle::ED25519(_) => KeyTag::Ed25519,
            PublicKeyHandle::SECP256K1(_) => KeyTag::Secp256k1,
            PublicKeyHandle::MlDsa65(_) => KeyTag::MlDsa65Hash,
        }
    }
```

**File:** docs/architecture/how/post_quantum_signatures.md (L82-87)
```markdown
The tag spaces are deliberately disjoint across the two types: `PublicKey`
owns `{0, 1, 2}`, `PublicKeyHandle` owns `{0, 1, 3}`. Tag `2` (full ML-DSA-65 key)
is reserved on `PublicKey` and is by construction never written into the
trie; tag `3` (hash) is reserved on `PublicKeyHandle` and never appears on the
wire. This makes "a full ML-DSA-65 key in the trie" and "an ML-DSA-65 hash
in a transaction" both unrepresentable at the type level.
```

**File:** docs/architecture/how/post_quantum_signatures.md (L100-105)
```markdown
| ED25519    | `[tag=0] \|\| 32-byte raw pubkey`                                    |
| SECP256K1  | `[tag=1] \|\| 64-byte raw pubkey`                                    |
| ML-DSA-65  | `[tag=3] \|\| sha3_256(domain_tag \|\| raw_pubkey)` (33 bytes total) |

Domain tag: `b"near:ml-dsa-65-pubkey-hash:v1"`, hashed before the pubkey
bytes. Prevents collisions with other SHA-3 uses in the protocol.
```

**File:** core/primitives/src/trie_key.rs (L328-339)
```rust
/// Append the on-trie identifier of `key_handle` into the given buffer.
/// The on-trie bytes are exactly `PublicKeyHandle`'s borsh encoding, so we
/// delegate to `BorshSerialize` rather than duplicating the layout here.
/// For ed25519 / secp256k1 the identifier is the full `PublicKey`; for
/// ML-DSA-65 it is `[tag=3] || sha3_256(domain || raw_pubkey)` - the
/// full ML-DSA-65 pubkey never enters the trie.
fn append_key_handle_trie_id(
    buf: &mut impl trie_key_buffer::TrieKeyBuffer,
    key_handle: &PublicKeyHandle,
) {
    borsh::to_writer(buf.borsh_writer(), key_handle).unwrap()
}
```
