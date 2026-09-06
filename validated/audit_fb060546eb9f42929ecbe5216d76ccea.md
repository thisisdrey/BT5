### Title
HTTP server continues servicing a connection after declaring `Connection: close` on a bad request - ([File: stackslib/src/net/server.rs])

### Summary
`HttpPeer::process_http_conversation` in `stackslib/src/net/server.rs` responds to a malformed/invalid HTTP request with a `400 Bad Request` reply whose preamble sets `keep_alive = false` (i.e., it tells the client `Connection: close`), but the code explicitly refrains from marking the connection dead (`convo_dead`) and continues to read and process further bytes from the same TCP socket on subsequent poll cycles. This mismatch between the declared connection-closure intent and the actual server behavior is the same bug class as the Waitress advisory (CWE-367/CWE-444): the server decides a request/connection should be terminated, but a race/omission in the state machine lets it keep servicing the socket, opening the door to HTTP request smuggling-style confusion between intermediaries and the origin node, or a client mixing malformed and well-formed pipelined data across what should be two separate connections.

### Finding Description
In `process_http_conversation` [1](#0-0) , when `convo.recv(client_sock)` returns `net_error::InvalidMessage` (a bad/undecodable HTTP message), the handler builds and queues a `400 Bad Request` response via `convo.reply_error(...)`, and the line that would mark the connection dead is explicitly commented out:

```
net_error::InvalidMessage => {
    info!("Got a bad HTTP message on socket {client_sock:?}");
    match convo.reply_error(...) {
        Ok(_) => {
            if let Err(e) = HttpPeer::saturate_http_socket(client_sock, convo) {
                info!(...);
                // convo_dead = true;
            }
        }
        ...
``` [2](#0-1) 

Meanwhile, `ConversationHttp::reply_error` (in `stackslib/src/net/rpc.rs`) explicitly sets `preamble.keep_alive = false;` on the outgoing error response, signaling to the client/any intermediary that the connection will be closed: [3](#0-2) 

Regardless of whether the connection was flagged dead, the function unconditionally calls `convo.chat(node_state)` afterward "to drain the conversation inbox," and if that succeeds, calls `HttpPeer::saturate_http_socket` to keep flushing/writing to the socket: [4](#0-3) 

The net effect: unless the *reply* itself fails to be built/queued, `convo_dead` remains `false` after an `InvalidMessage` error, so the peer/event loop (`process_ready_sockets` → `run`) will keep this socket registered and continue calling `recv()`/`chat()` on it in future poll iterations, i.e., the connection is never actually closed by this codepath despite the reply's `Connection: close` header telling the client (and any downstream cache/proxy honoring that header) otherwise.

This directly parallels the Waitress flaw: an error/parse-failure path that *should* result in connection teardown instead permits continued processing of subsequent bytes/messages on the same socket, because the "close" decision and the actual socket lifecycle are decoupled and can diverge.

### Impact Explanation
This breaks the equality between "declared connection state" (client is told `Connection: close`) and "actual connection state" (socket stays open and continues to be serviced). Concretely:
- A remote, unauthenticated client can send one malformed HTTP message followed immediately (pipelined, on the same TCP connection) by additional bytes/requests. Because the connection is not actually closed after the 400 response, those subsequent bytes are parsed and processed as new requests on what the client (or any proxy honoring `Connection: close`) believes is (or should be) a terminated connection.
- If Stacks nodes are ever fronted by reverse proxies/load balancers that pool connections and rely on `Connection: close` semantics to decide connection reuse boundaries, this discrepancy can enable HTTP request-smuggling-style cross-request confusion (CWE-444), where data intended for one logical request/connection bleeds into processing of a subsequent one.
- At minimum, this represents a resource-management/consistency defect that violates the server's own declared intent and could be leveraged to keep sockets and associated per-connection state (`reply_streams`, `pending_request`, etc.) alive/exploitable longer than the server itself decided was safe, contradicting the “received bad data → shut it down” design the surrounding comments describe elsewhere (e.g., `chat.rs`'s `validate_inbound_message`, which does the correct thing by returning `Err` to force disconnection on `InvalidMessage` at the p2p layer, `stackslib/src/net/chat.rs:2501-2515`).

Given the rules' impact tiers, this best matches "request smuggling or auth bypass" (Critical) if an intermediary is present, or a correctness/DoS-adjacent connection-state bug in the direct case; it is a genuine deviation from the intended behavior of closing on bad input, reachable with a single crafted HTTP message from any remote peer.

### Likelihood Explanation
Likelihood is high for the base behavioral flaw (no special conditions needed — the `// convo_dead = true;` line is unconditionally commented out for the success path of building/sending the 400 reply), so any single malformed HTTP request against the node's RPC HTTP server reproduces the "still-open-despite-close-header" state. Whether this is exploitable as full request smuggling depends on external topology (a proxy/LB in front of the node that trusts `Connection: close`), which I could not verify further within this repo (no proxy component exists in-repo); absent that, the direct impact is a self-inconsistent connection lifecycle bug on the node's own listener.

### Recommendation
In `process_http_conversation` (`stackslib/src/net/server.rs`), when the reply's preamble sets `keep_alive = false` (as `reply_error` does for `InvalidMessage`), unconditionally set `convo_dead = true` after successfully flushing the error response, rather than leaving the commented-out line in place. More generally, tie the socket-teardown decision (`convo_dead`) directly to the `keep_alive` flag of whatever response was just sent, so the actual connection lifecycle can never diverge from what was communicated to the client.

### Proof of Concept
1. Open a TCP connection to the node's RPC HTTP port.
2. Send a single malformed HTTP request (e.g., an invalid `Connection:` header value, as covered by `stackslib/src/net/http/tests.rs:195-234`, `"GET /foo HTTP/1.1\r\nHost: localhost:8080\r\nConnection: foo\r\n\r\n"`), causing `consume_payload`/`read_preamble` to return `net_error::InvalidMessage` up through `convo.recv()`.
3. Observe the node replies with `400 Bad Request` and a `Connection: close` header (per `reply_error`'s `keep_alive = false`).
4. Without closing the TCP connection, immediately send a second, well-formed HTTP request (e.g. `GET /v2/info HTTP/1.1\r\nHost: ...\r\n\r\n`) on the same socket.
5. Observe the node processes and responds to the second request normally on the same connection — proving the socket was never actually closed despite promising `Connection: close`, confirming the divergence between declared and actual connection lifecycle described above.

### Citations

**File:** stackslib/src/net/server.rs (L418-466)
```rust
    fn process_http_conversation(
        node_state: &mut StacksNodeState,
        event_id: usize,
        client_sock: &mut mio_net::TcpStream,
        convo: &mut ConversationHttp,
    ) -> (bool, Vec<StacksMessageType>) {
        // get incoming bytes and update the state of this conversation.
        let mut convo_dead = false;
        let recv_res = convo.recv(client_sock);
        if let Err(e) = recv_res {
            match e {
                net_error::PermanentlyDrained => {
                    // socket got closed, but we might still have pending unsolicited messages
                    debug!(
                        "Remote HTTP peer disconnected event {} (socket {:?})",
                        event_id, &client_sock
                    );
                    convo_dead = true;
                }
                net_error::InvalidMessage => {
                    // got sent bad data.  If this was an inbound conversation, send it a HTTP
                    // 400 and close the socket.
                    info!("Got a bad HTTP message on socket {client_sock:?}");
                    match convo.reply_error(StacksHttpResponse::new_empty_error(
                        &HttpBadRequest::new(
                            "Received an HTTP message that the node could not decode".to_string(),
                        ),
                    )) {
                        Ok(_) => {
                            // prime the socket
                            if let Err(e) = HttpPeer::saturate_http_socket(client_sock, convo) {
                                info!("Failed to flush HTTP 400 to socket {client_sock:?}: {e:?}",);
                                // convo_dead = true;
                            }
                        }
                        Err(e) => {
                            info!("Failed to reply HTTP 400 to socket {client_sock:?}: {e:?}",);
                            convo_dead = true;
                        }
                    }
                }
                _ => {
                    info!(
                        "Failed to receive HTTP data on event {event_id} (socket {client_sock:?}): {e:?}",
                    );
                    convo_dead = true;
                }
            }
        }
```

**File:** stackslib/src/net/server.rs (L468-491)
```rust
        // react to inbound messages -- do we need to send something out, or fulfill requests
        // to other threads?  Try to chat even if the recv() failed, since we'll want to at
        // least drain the conversation inbox.
        let msgs = match convo.chat(node_state) {
            Ok(msgs) => msgs,
            Err(e) => {
                info!(
                    "Failed to converse HTTP on event {event_id} (socket {client_sock:?}): {e:?}",
                );
                convo_dead = true;
                vec![]
            }
        };

        if !convo_dead {
            // (continue) sending out data in this conversation, if the conversation is still
            // ongoing
            if let Err(e) = HttpPeer::saturate_http_socket(client_sock, convo) {
                info!(
                    "Failed to send HTTP data to event {event_id} (socket {client_sock:?}): {e:?}",
                );
                convo_dead = true;
            }
        }
```

**File:** stackslib/src/net/rpc.rs (L196-211)
```rust
        }
        let (mut preamble, body_contents) = res.try_into_contents()?;
        preamble.content_length = body_contents.content_length();
        preamble.keep_alive = false;

        // account for the request
        self.total_request_count += 1;

        // make the relay handle. There may not have been a valid request in the first place, so
        // we'll use a relay handle (not a reply handle) to push out the error.
        let mut reply = self.connection.make_relay_handle(self.conn_id)?;

        // queue up the HTTP headers, and then stream back the body.
        preamble.consensus_serialize(&mut reply)?;
        self.reply_streams.push_back((reply, body_contents, false));
        Ok(())
```
