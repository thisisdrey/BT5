### Title
Unauthenticated re-key via `ConversationP2P::validate_handshake` Some(_) branch skips signature verification - ([File: stackslib/src/net/chat.rs])

### Summary
`validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` in the `None` branch (bootstrap case, when no key is on file yet). In the `Some(_)` branch — i.e. when the conversation already has an established public key `K1` — the function performs only an address/port sanity check for outbound connections and never verifies the incoming message's signature against either the old key `K1` or the newly-claimed key `K2`.

### Finding Description
The function reads: [1](#0-0) 

In the `None` arm, the code verifies the Handshake's signature against the self-declared `handshake_data.node_public_key`, which is the only sane thing to do the first time a peer's key is learned. In the `Some(_)` arm, no call to `verify_secp256k1` (or any signature check) occurs at all — the branch body only compares `handshake_data.addrbytes`/`port` against the socket address, and only for outbound connections. After `validate_handshake` returns `Ok(())`, the caller proceeds to `update_from_handshake_data`, whose stated purpose is to learn/replace the session's public key: [2](#0-1) 

Because no cryptographic check ties the second Handshake's claimed `node_public_key` (`K2`) to the identity already bound to the session (`K1`), an attacker who has completed an initial Handshake establishing `K1` (trivial — they choose `K1` and sign with it themselves in the `None` branch) can send a second Handshake payload declaring an arbitrary `node_public_key = K2` with an arbitrary (even self-consistent but unrelated) signature, and the `Some(_)` branch will accept it as long as the address/port field matches or the connection is inbound (for inbound connections there is no address check at all). This breaks the equality the question raises: `message.preamble.signature` recovering to `handshake_data.node_public_key` is never checked against `self.connection.get_public_key()` (`K1`) nor is the signature checked to recover `K2` at all in this branch.

### Impact Explanation
If `update_from_handshake_data` blindly adopts the new `node_public_key` once `validate_handshake` returns `Ok`, this allows an unauthenticated remote peer to rewrite the cryptographic identity bound to an already-established P2P session mid-conversation, without proving possession of the new private key. This can be used to impersonate a different node's public key onto an attacker-controlled socket (identity forgery on an existing session), corrupting neighbor-table/reputation entries keyed by public key hash and potentially enabling further downstream trust decisions (e.g., allow/deny-listing, StackerDB writer identity, or reputation tracking) to be misattributed to a victim's key. This is repeatable per-connection and requires only a couple of crafted Handshake messages over a normal P2P TCP connection.

### Likelihood Explanation
The attacker only needs to be an unprivileged remote peer able to open a P2P connection (no special role, secret, or privileged access required). Preconditions: complete an initial Handshake (attacker self-selects `K1` and self-signs, which always succeeds in the `None` branch), then send a second Handshake with a different `node_public_key` field. For inbound connections (the common case for a listening node), there is no address/port gating either, making the Some(_) branch a no-op check.

### Recommendation
In the `Some(_)` branch of `validate_handshake`, always verify the incoming message's signature. Specifically: verify `message.verify_secp256k1(&handshake_data.node_public_key)` unconditionally (as in the `None` branch) so a re-key attempt must be signed by the new key, and/or require that a re-key be authorized by a signature from the *previously* established key `K1` before accepting a switch to `K2`, rejecting otherwise with `net_error::InvalidMessage`.

### Proof of Concept
Add a test in `stackslib/src/net/chat.rs`'s test module that:
1. Constructs a `ConversationP2P` for a mock peer, drives an initial Handshake with `node_public_key = K1`, signed with `K1`, and asserts `self.connection.get_public_key() == Some(K1)`.
2. Crafts a second `StacksMessage::Handshake` with `node_public_key = K2` (a different keypair), signs it with an arbitrary/self-referential key (not `K1`), and feeds it through `validate_handshake`/`update_from_handshake_data`.
3. Assert that this call either returns `Ok(())`/updates `self.connection.get_public_key()` to `Some(K2)` (demonstrating the forged re-key succeeds) — confirming the missing-verification bug — versus the expected/fixed behavior of returning `Err(net_error::InvalidMessage)`.

### Citations

**File:** stackslib/src/net/chat.rs (L1058-1092)
```rust
        match self.connection.get_public_key() {
            None => {
                // if we don't yet have a public key for this node, verify the message.
                // if it's improperly signed, it's probably a poorly-timed re-key request (but either way the message should be rejected)
                message
                    .verify_secp256k1(&handshake_data.node_public_key)
                    .map_err(|_e| {
                        debug!(
                            "{:?}: invalid handshake: not signed with given public key",
                            &self
                        );
                        net_error::InvalidMessage
                    })?;
            }
            Some(_) => {
                // for outbound connections, the self-reported address must match socket address if we already have a public key.
                // (not the case for inbound connections, since the peer socket address we see may
                // not be the same as the address the remote peer thinks it has).
                // The only exception to this is if the remote peer does not yet know its own
                // public IP address, in which case, its handshake addrbytes will be the
                // any-network bind address (0.0.0.0 or ::)
                if self.stats.outbound
                    && (!handshake_data.addrbytes.is_anynet()
                        && (self.peer_addrbytes != handshake_data.addrbytes
                            || self.peer_port != handshake_data.port))
                {
                    // wrong peer address
                    debug!(
                        "{:?}: invalid handshake -- wrong addr/port ({:?}:{:?})",
                        &self, &handshake_data.addrbytes, handshake_data.port
                    );
                    return Err(net_error::InvalidHandshake);
                }
            }
        };
```

**File:** stackslib/src/net/chat.rs (L1129-1150)
```rust
    /// Update connection state from handshake data.
    /// Returns true if we learned a new public key; false if not
    pub fn update_from_handshake_data(
        &mut self,
        preamble: &Preamble,
        handshake_data: &HandshakeData,
    ) -> Result<bool, net_error> {
        let pubk = handshake_data
            .node_public_key
            .to_public_key()
            .map_err(|e| net_error::DeserializeError(e.into()))?;

        self.peer_version = preamble.peer_version;
        self.peer_network_id = preamble.network_id;
        self.peer_services = handshake_data.services;
        self.peer_expire_block_height = handshake_data.expire_block_height;
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();

        let mut updated = false;
        let cur_pubk_opt = self.connection.get_public_key();
```
