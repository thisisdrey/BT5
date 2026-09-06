[1](#0-0) [2](#0-1)

### Citations

**File:** stackslib/src/net/codec.rs (L1-48)
```rust
// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
// Copyright (C) 2020-2026 Stacks Open Internet Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.

use std::collections::HashSet;
use std::io;
use std::io::Read;

use clarity::vm::types::QualifiedContractIdentifier;
use clarity::vm::ContractName;
use rand::{self, Rng};
use sha2::{Digest, Sha512_256};
use stacks_common::bitvec::BitVec;
use stacks_common::codec::{
    read_next, read_next_at_most, read_next_exact, write_next, Error as codec_error,
    StacksMessageCodec, MAX_MESSAGE_LEN, MAX_RELAYERS_LEN, PREAMBLE_ENCODED_SIZE,
};
use stacks_common::types::chainstate::{BlockHeaderHash, BurnchainHeaderHash};
use stacks_common::types::net::PeerAddress;
use stacks_common::types::StacksPublicKeyBuffer;
use stacks_common::util::hash::{to_hex, Hash160};
use stacks_common::util::retry::BoundReader;
use stacks_common::util::secp256k1::{MessageSignature, Secp256k1PrivateKey, Secp256k1PublicKey};

use crate::burnchains::{BurnchainView, PrivateKey, PublicKey};
use crate::chainstate::burn::ConsensusHash;
use crate::chainstate::nakamoto::NakamotoBlock;
use crate::chainstate::stacks::{
    StacksBlock, StacksMicroblock, StacksPublicKey, StacksTransaction, MAX_BLOCK_LEN,
};
use crate::net::db::LocalPeer;
use crate::net::{Error as net_error, *};

pub fn bitvec_len(bitlen: u16) -> u16 {
    (bitlen / 8) + (if bitlen % 8 != 0 { 1 } else { 0 })
}
```

**File:** stackslib/src/net/connection.rs (L1-48)
```rust
// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
// Copyright (C) 2020-2026 Stacks Open Internet Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.

use std::collections::{HashMap, VecDeque};
use std::io;
use std::io::{Read, Write};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError};
use std::time::Duration;

use clarity::vm::costs::ExecutionCost;
use clarity::vm::types::{QualifiedContractIdentifier, BOUND_VALUE_SERIALIZATION_HEX};
use stacks_common::codec::MAX_MESSAGE_LEN;
use stacks_common::types::net::PeerAddress;
use stacks_common::util::get_epoch_time_secs;
use stacks_common::util::pipe::*;
use stacks_common::util::secp256k1::Secp256k1PublicKey;

use crate::config::{DEFAULT_PROPOSAL_MEMORY_BYTES, DEFAULT_READ_ONLY_CALL_MAX_MEM_BYTES};
use crate::monitoring::{update_inbound_bandwidth, update_outbound_bandwidth};
use crate::net::download::BLOCK_DOWNLOAD_INTERVAL;
use crate::net::inv::{INV_REWARD_CYCLES, INV_SYNC_INTERVAL};
use crate::net::neighbors::{
    MAX_NEIGHBOR_AGE, NEIGHBOR_REQUEST_TIMEOUT, NEIGHBOR_WALK_INTERVAL, NUM_INITIAL_WALKS,
    WALK_MAX_DURATION, WALK_MIN_DURATION, WALK_RESET_INTERVAL, WALK_RESET_PROB, WALK_RETRY_COUNT,
    WALK_SEED_PROBABILITY, WALK_STATE_TIMEOUT,
};
use crate::net::{
    Error as net_error, MessageSequence, NeighborAddress, ProtocolFamily, StacksHttp, StacksP2P,
};

/// The default maximum age in seconds of a block that can be validated by the block proposal endpoint
pub const DEFAULT_BLOCK_PROPOSAL_MAX_AGE_SECS: u64 = 600;

/// The default maximum time to spend validating a block proposal in seconds
pub const DEFAULT_BLOCK_PROPOSAL_VALIDATION_TIMEOUT_SECS: u64 = 60;
```
