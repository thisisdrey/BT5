### Title
Reliability score inflated by hash-mismatched attachments enabling malicious peer priority capture - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` calls `report.bump_successful_requests()` whenever `response.decode_atlas_get_attachment()` succeeds, without ever checking that the decoded `Attachment`'s hash matches the `content_hash` of the `AttachmentRequest` that was sent. A malicious outbound peer can therefore answer every `GetAttachment` request with a well-formed but content-mismatched attachment and accumulate a perfect `ReliabilityReport::score()`, since `bump_failed_requests()` is only invoked on transport/parse failure, never on hash mismatch.

### Finding Description
`extend_with_attachments` in `stackslib/src/net/atlas/download.rs` is: [1](#0-0) 
For each successfully decoded response it does `self.attachments.insert(response.attachment); report.bump_successful_requests();` with no comparison against `request.content_hash`. Compare this with `AttachmentsBatch::resolve_attachment`, which is the *only* place a hash equality check happens, and that check occurs later in `AttachmentsDownloader::run` — after the reliability report has already been updated: [2](#0-1) [3](#0-2) 

`ReliabilityReport` itself has no notion of correctness, only sent/success counters and a score: [4](#0-3) 

That score directly drives peer selection: `AttachmentRequest::get_most_reliable_source` picks the `max_by_key(|(_, v)| v.score())` peer as the request target for all subsequent fetches of *any* content hash, and `Ord for AttachmentRequest`/`Ord for AttachmentsInventoryRequest` also sort by this score: [5](#0-4) [6](#0-5) 

So an attacker peer that is an outbound sync target (`network.get_outbound_sync_peers()`, no secret/signature required) can reply to every `GetAttachment` request with any syntactically valid attachment payload (arbitrary bytes that merely decode successfully via `decode_atlas_get_attachment`), regardless of matching the requested `content_hash`, and its `ReliabilityReport::total_requests_sent`/`total_requests_success` will both increment identically to an honest peer that actually serves correct content. Over repeated rounds this equalizes or exceeds honest peers' scores, letting the attacker win `get_most_reliable_source()`/priority-queue ordering for all future attachment and inventory requests across every tracked BNS name.

I was unable to fully inspect `decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs`) in this session due to iteration limits, but the ordering of operations in `extend_with_attachments` (score bump happens strictly before/independent of the later `resolve_attachment` hash check in `run()`) is confirmed directly from the code shown above, which is sufficient to establish the broken equality regardless of decoder internals.

### Impact Explanation
This does not directly forge state written to the AtlasDB as canonical (the mismatched `Attachment` is still subject to `resolve_attachment`'s hash filter before being paired to any `AttachmentInstance`), but it does let a single unprivileged remote peer monopolize the victim's attachment-fetch priority ordering, causing legitimate peers to be starved of requests and BNS attachment resolution to stall/degrade node-wide — a persistent, repeatable denial-of-service condition on BNS resolution driven entirely by peer-selection logic, matching the "steering a node off canonical sourcing via false inventory/reliability" pattern described as High impact.

### Likelihood Explanation
Preconditions are minimal: the attacker just needs to be an outbound sync peer that the victim node dials (no handshake secret, no StackerDB ownership, no privileged role) and respond to `GetAttachment` HTTP requests with arbitrary well-formed-but-wrong-content payloads. This is trivially repeatable per round of the `AttachmentsBatchStateMachine` and costs the attacker nothing beyond running an ordinary reachable peer.

### Recommendation
In `extend_with_attachments`, verify `response.attachment.hash() == request.content_hash` before calling `report.bump_successful_requests()`; call `report.bump_failed_requests()` (or a distinct "bad content" penalty) when the hash does not match, and only insert the attachment into `self.attachments` when it does match.

### Proof of Concept
Add a Rust unit test in `stackslib/src/net/atlas/download.rs` (or a new test module) that:
1. Constructs an `AttachmentsBatchStateContext` with two peers, `honest_url` and `attacker_url`, each with `ReliabilityReport::empty()`.
2. Builds a `BatchedRequestsResult<AttachmentRequest>` where, for several rounds, `attacker_url`'s response decodes to an `Attachment` whose `.hash()` != `request.content_hash`, while `honest_url`'s response decodes to the correct attachment.
3. Calls `context.extend_with_attachments(&mut results)` for each round.
4. Asserts `context.peers[&attacker_url].score() >= context.peers[&honest_url].score()` despite the attacker never having delivered a matching attachment — demonstrating the broken equality at `report.bump_successful_requests()` in `extend_with_attachments` (download.rs lines 547-552).

### Citations

**File:** stackslib/src/net/atlas/download.rs (L152-169)
```rust
        match progress {
            AttachmentsBatchStateMachine::Done(ref mut context) => {
                for attachment in context.attachments.drain() {
                    let attachments_instances = network
                        .atlasdb
                        .find_all_attachment_instances(&attachment.hash())
                        .map_err(net_error::DBError)?;
                    network
                        .atlasdb
                        .insert_instantiated_attachment(&attachment)
                        .map_err(net_error::DBError)?;
                    for attachment_instance in attachments_instances.into_iter() {
                        resolved_attachments.push((attachment_instance, attachment.clone()));
                    }
                    context
                        .attachments_batch
                        .resolve_attachment(&attachment.hash())
                }
```

**File:** stackslib/src/net/atlas/download.rs (L530-558)
```rust
    pub fn extend_with_attachments(
        mut self,
        results: &mut BatchedRequestsResult<AttachmentRequest>,
    ) -> AttachmentsBatchStateContext {
        for (request, response) in results.succeeded.drain() {
            let report = self
                .peers
                .get_mut(request.get_url())
                .expect("Atlas: unable to retrieve reliability report for peer");

            let response = if let Some(r) = response {
                r
            } else {
                report.bump_failed_requests();
                continue;
            };

            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
        }
        let mut events_ids = results.faulty_peers.keys().copied().collect::<Vec<usize>>();
        self.events_to_deregister.append(&mut events_ids);

        self
    }
```

**File:** stackslib/src/net/atlas/download.rs (L1016-1020)
```rust
impl Ord for AttachmentsInventoryRequest {
    fn cmp(&self, other: &AttachmentsInventoryRequest) -> Ordering {
        self.reliability_report.cmp(&other.reliability_report)
    }
}
```

**File:** stackslib/src/net/atlas/download.rs (L1073-1096)
```rust
impl AttachmentRequest {
    pub fn get_most_reliable_source(&self) -> (&UrlString, &ReliabilityReport) {
        self.sources
            .iter()
            .max_by_key(|(_, v)| v.score())
            .expect("Atlas: trying to select an Url out of an empty set")
    }
}

impl Hash for AttachmentRequest {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.content_hash.hash(state)
    }
}

impl Ord for AttachmentRequest {
    fn cmp(&self, other: &AttachmentRequest) -> Ordering {
        other.sources.len().cmp(&self.sources.len()).then_with(|| {
            let (_, report) = self.get_most_reliable_source();
            let (_, other_report) = other.get_most_reliable_source();
            report.cmp(other_report)
        })
    }
}
```

**File:** stackslib/src/net/atlas/download.rs (L1227-1239)
```rust
    pub fn resolve_attachment(&mut self, content_hash: &Hash160) {
        for missing_attachments in self.attachments_instances.values_mut() {
            let mut keys = vec![];
            for (k, hash) in missing_attachments.iter() {
                if hash == content_hash {
                    keys.push(*k);
                }
            }
            for key in keys {
                missing_attachments.remove(&key);
            }
        }
    }
```

**File:** stackslib/src/net/atlas/download.rs (L1268-1306)
```rust
pub struct ReliabilityReport {
    pub total_requests_sent: u32,
    pub total_requests_success: u32,
}

impl ReliabilityReport {
    pub fn bump_successful_requests(&mut self) {
        self.total_requests_sent += 1;
        self.total_requests_success += 1;
    }

    pub fn bump_failed_requests(&mut self) {
        self.total_requests_sent += 1;
    }
}

impl ReliabilityReport {
    pub fn new(total_requests_sent: u32, total_requests_success: u32) -> ReliabilityReport {
        ReliabilityReport {
            total_requests_sent,
            total_requests_success,
        }
    }

    pub fn empty() -> ReliabilityReport {
        ReliabilityReport {
            total_requests_sent: 0,
            total_requests_success: 0,
        }
    }

    pub fn score(&self) -> u32 {
        let n = self.total_requests_sent;
        if n == 0 {
            return n;
        }
        self.total_requests_success * 1000 / (n * 1000) + n
    }
}
```
