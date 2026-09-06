### No vulnerability found for this question.

`SlotMetadata::new_unsigned` is a trivial in-memory constructor that builds a `SlotMetadata` struct with an empty `MessageSignature` from caller-supplied `slot_id`, `slot_version`, and `data_hash` values [1](#0-0) . It performs no I/O, no network relay, no deduplication logic, and is not itself reachable from a TCP connection — it is only used internally (e.g., by tests or code that later calls `.sign()`) to build an unsigned metadata object before signing [2](#0-1) . There is no relay/broadcast code path inside `libstackerdb.rs` at all; the file only defines `SlotMetadata`, `StackerDBChunkData`, `StackerDBChunkAckData`, their (de)serialization, and helper path-building functions [3](#0-2) . Since the claimed exploit ("a relay path re-broadcasts without dedup") requires a relay/dedup mechanism that simply does not exist in or near `new_unsigned`, the premise of the question does not hold against this function.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L70-325)
```rust
/// Slot metadata from the DB.
/// This is derived state from a StackerDBChunkData message.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SlotMetadata {
    /// Slot identifier (unique for each DB instance)
    pub slot_id: u32,
    /// Slot version (a lamport clock)
    pub slot_version: u32,
    /// data hash
    pub data_hash: Sha512Trunc256Sum,
    /// signature over the above
    pub signature: MessageSignature,
}

/// Stacker DB chunk (i.e. as a reply to a chunk request)
#[derive(Clone, PartialEq, Serialize, Deserialize)]
pub struct StackerDBChunkData {
    /// slot ID
    pub slot_id: u32,
    /// slot version (a lamport clock)
    pub slot_version: u32,
    /// signature from the stacker over (slot id, slot version, chunk sha512/256)
    pub sig: MessageSignature,
    /// the chunk data
    #[serde(
        serialize_with = "stackerdb_chunk_hex_serialize",
        deserialize_with = "stackerdb_chunk_hex_deserialize"
    )]
    pub data: Vec<u8>,
}

impl fmt::Debug for StackerDBChunkData {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.data.len() < 128 {
            write!(
                f,
                "StackerDBChunkData({},{},{},{})",
                self.slot_id,
                self.slot_version,
                self.sig,
                to_hex(&self.data)
            )
        } else {
            write!(
                f,
                "StackerDBChunkData({},{},{},{}...({}))",
                self.slot_id,
                self.slot_version,
                self.sig,
                to_hex(&self.data[..128]),
                self.data.len()
            )
        }
    }
}

/// StackerDB post chunk acknowledgement
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StackerDBChunkAckData {
    pub accepted: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<SlotMetadata>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<u32>,
}

impl fmt::Display for StackerDBChunkAckData {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}

impl SlotMetadata {
    /// Make a new unsigned slot metadata
    pub fn new_unsigned(
        slot_id: u32,
        slot_version: u32,
        data_hash: Sha512Trunc256Sum,
    ) -> SlotMetadata {
        SlotMetadata {
            slot_id,
            slot_version,
            data_hash,
            signature: MessageSignature::empty(),
        }
    }

    /// Get the digest to sign that authenticates this chunk data and metadata
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }

    /// Sign this slot metadata, committing to slot_id, slot_version, and
    /// data_hash.  Sets self.signature to the signature.
    /// Fails if the underlying crypto library fails
    pub fn sign(&mut self, privkey: &StacksPrivateKey) -> Result<(), Error> {
        let auth_digest = self.auth_digest();
        let sig = privkey
            .sign(&auth_digest.0)
            .map_err(|se| Error::SigningError(se.to_string()))?;

        self.signature = sig;
        Ok(())
    }

    /// Verify that a given principal signed this chunk metadata.
    /// Note that the address version is ignored.
    pub fn verify(&self, principal: &StacksAddress) -> Result<bool, Error> {
        let sigh = self.auth_digest();
        let pubk = StacksPublicKey::recover_to_pubkey_without_validating_low_s(
            sigh.as_bytes(),
            &self.signature,
        )
        .map_err(|ve| Error::VerifyingError(ve.to_string()))?;

        let pubkh = Hash160::from_node_public_key(&pubk);
        Ok(pubkh == *principal.bytes())
    }
}

/// Helper methods for StackerDBChunkData messages
impl StackerDBChunkData {
    /// Create a new StackerDBChunkData instance.
    pub fn new(slot_id: u32, slot_version: u32, data: Vec<u8>) -> StackerDBChunkData {
        StackerDBChunkData {
            slot_id,
            slot_version,
            sig: MessageSignature::empty(),
            data,
        }
    }

    /// Calculate the hash of the chunk bytes.  This is the SHA512/256 hash of the data.
    pub fn data_hash(&self) -> Sha512Trunc256Sum {
        Sha512Trunc256Sum::from_data(&self.data)
    }

    /// Create an owned SlotMetadata describing the metadata of this slot.
    pub fn get_slot_metadata(&self) -> SlotMetadata {
        SlotMetadata {
            slot_id: self.slot_id,
            slot_version: self.slot_version,
            data_hash: self.data_hash(),
            signature: self.sig.clone(),
        }
    }

    /// Sign this given chunk data message with the given private key.
    /// Sets self.signature to the signature.
    /// Fails if the underlying signing library fails.
    pub fn sign(&mut self, privk: &StacksPrivateKey) -> Result<(), Error> {
        let mut md = self.get_slot_metadata();
        md.sign(privk)?;
        self.sig = md.signature;
        Ok(())
    }

    pub fn recover_pk(&self) -> Result<StacksPublicKey, Error> {
        let digest = self.get_slot_metadata().auth_digest();
        StacksPublicKey::recover_to_pubkey_without_validating_low_s(digest.as_bytes(), &self.sig)
            .map_err(|ve| Error::VerifyingError(ve.to_string()))
    }

    /// Verify that this chunk was signed by the given
    /// public key hash (`addr`).  Only fails if the underlying signing library fails.
    pub fn verify(&self, addr: &StacksAddress) -> Result<bool, Error> {
        let md = self.get_slot_metadata();
        md.verify(addr)
    }
}

impl StacksMessageCodec for StackerDBChunkData {
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), CodecError> {
        write_next(fd, &self.slot_id)?;
        write_next(fd, &self.slot_version)?;
        write_next(fd, &self.sig)?;
        write_next(fd, &self.data)?;
        Ok(())
    }

    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<StackerDBChunkData, CodecError> {
        let slot_id: u32 = read_next(fd)?;
        let slot_version: u32 = read_next(fd)?;
        let sig: MessageSignature = read_next(fd)?;
        let data: Vec<u8> = read_next_at_most(fd, STACKERDB_MAX_CHUNK_SIZE)?;
        Ok(StackerDBChunkData {
            slot_id,
            slot_version,
            sig,
            data,
        })
    }
}

fn stackerdb_chunk_hex_serialize<S: serde::Serializer>(
    chunk: &[u8],
    s: S,
) -> Result<S::Ok, S::Error> {
    let inst = to_hex(chunk);
    s.serialize_str(inst.as_str())
}

fn stackerdb_chunk_hex_deserialize<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> Result<Vec<u8>, D::Error> {
    let inst_str = String::deserialize(d)?;
    hex_bytes(&inst_str).map_err(serde::de::Error::custom)
}

/// Calculate the GET path for a stacker DB metadata listing
pub fn stackerdb_get_metadata_path(contract_id: QualifiedContractIdentifier) -> String {
    format!(
        "/v2/stackerdb/{}/{}",
        StacksAddress::from(contract_id.issuer),
        contract_id.name
    )
}

/// Calculate the GET path for a stacker DB chunk
pub fn stackerdb_get_chunk_path(
    contract_id: QualifiedContractIdentifier,
    slot_id: u32,
    slot_version: Option<u32>,
) -> String {
    if let Some(version) = slot_version {
        format!(
            "/v2/stackerdb/{}/{}/{}/{}",
            StacksAddress::from(contract_id.issuer),
            contract_id.name,
            slot_id,
            version
        )
    } else {
        format!(
            "/v2/stackerdb/{}/{}/{}",
            StacksAddress::from(contract_id.issuer),
            contract_id.name,
            slot_id
        )
    }
}

/// Calculate POST path for a stacker DB chunk
pub fn stackerdb_post_chunk_path(contract_id: QualifiedContractIdentifier) -> String {
    format!(
        "/v2/stackerdb/{}/{}/chunks",
        StacksAddress::from(contract_id.issuer),
        contract_id.name
    )
}
```
