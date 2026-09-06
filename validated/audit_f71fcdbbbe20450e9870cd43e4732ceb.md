[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** stackslib/src/net/unsolicited.rs (L56-80)
```rust
    fn check_peer_authenticated(&self, event_id: usize) -> Option<NeighborKey> {
        let Some((remote_neighbor_key, remote_is_authenticated)) = self
            .peers
            .get(&event_id)
            .map(|convo| (convo.to_neighbor_key(), convo.is_authenticated()))
        else {
            test_debug!(
                "{:?}: No such neighbor event={}",
                &self.get_local_peer(),
                event_id
            );
            return None;
        };

        if !remote_is_authenticated {
            // drop -- a correct peer will have authenticated before sending this message
            test_debug!(
                "{:?}: Unauthenticated neighbor {:?}",
                &self.get_local_peer(),
                &remote_neighbor_key
            );
            return None;
        }
        Some(remote_neighbor_key)
    }
```

**File:** stackslib/src/net/unsolicited.rs (L546-577)
```rust
    ) -> (bool, bool) {
        match payload {
            StacksMessageType::StackerDBPushChunk(ref data) => {
                // N.B. send back a reply if we're calling to buffer, since this would be the first
                // time we're seeing this message (instead of a subsequent time on follow-up
                // processing).
                let (can_buffer, can_store) = self
                    .handle_unsolicited_StackerDBPushChunk(
                        chainstate, event_id, preamble, data, buffer,
                    )
                    .unwrap_or_else(|e| {
                        info!(
                            "{:?}: failed to handle unsolicited {:?} when buffer = {}: {:?}",
                            self.get_local_peer(),
                            payload,
                            buffer,
                            &e
                        );
                        (false, false)
                    });
                if buffer && can_buffer && !can_store {
                    debug!(
                        "{:?}: Buffering {:?} to retry on next sortition",
                        self.get_local_peer(),
                        &payload
                    );
                }
                (can_buffer, can_store)
            }
            _ => (false, true),
        }
    }
```

**File:** stackslib/src/net/unsolicited.rs (L580-619)
```rust
    pub fn authenticate_unsolicited_messages(
        &self,
        unsolicited: HashMap<usize, Vec<StacksMessage>>,
    ) -> PendingMessages {
        unsolicited.into_iter().filter_map(|(event_id, messages)| {
            if messages.is_empty() {
                // no messages for this event
                return None;
            }
            if self.check_peer_authenticated(event_id).is_none() {
                if cfg!(test)
                    && self
                        .connection_opts
                        .test_disable_unsolicited_message_authentication
                {
                    test_debug!(
                        "{:?}: skip unsolicited message authentication",
                        &self.get_local_peer()
                    );
                } else {
                    debug!("Will not handle unsolicited messages from unauthenticated or dead event {}", event_id);
                    return None;
                }
            };
            let Some(convo) = self.peers.get(&event_id) else {
                debug!(
                    "{:?}: No longer such neighbor event={}, dropping {} unsolicited messages",
                    &self.get_local_peer(),
                    event_id,
                    messages.len()
                );
                return None;
            };
            Some((
                (event_id, convo.to_neighbor_key()),
                PendingMessagesFrom::new(convo.to_neighbor_address(), messages),
            ))
        })
        .collect()
    }
```
