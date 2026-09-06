[1](#0-0) [2](#0-1)

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L79-89)
```rust
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GetAttachmentsInvResponse {
    pub block_id: StacksBlockId,
    pub pages: Vec<AttachmentPage>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttachmentPage {
    pub index: u32,
    pub inventory: Vec<u8>,
}
```

**File:** stackslib/src/net/atlas/mod.rs (L184-184)
```rust
    const ATTACHMENTS_INV_PAGE_SIZE: u32 = 64;
```
