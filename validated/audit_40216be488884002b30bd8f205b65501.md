[1](#0-0)

### Citations

**File:** poh/src/poh_recorder.rs (L100-120)
```rust
pub type WorkingBankEntryOrMarker = (Arc<Bank>, (EntryOrMarker, u64));

#[derive(Debug)]
pub struct RecordSummary {
    pub remaining_hashes_in_slot: u64,
}

pub struct Record {
    pub mixin: Hash,
    pub transactions: Vec<VersionedTransaction>,
    pub bank_id: BankId,
}

impl Record {
    pub fn new(mixin: Hash, transactions: Vec<VersionedTransaction>, bank_id: BankId) -> Self {
        Self {
            mixin,
            transactions,
            bank_id,
        }
    }
```
