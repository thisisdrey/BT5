import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'ethereum/go-ethereum'
# todo: the name of the repository
REPO_NAME = 'go-ethereum'

run_number = os.environ.get('GITHUB_RUN_NUMBER', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"


scope_files = [
    # =================================================================================
    # LENS: CONSENSUS STATE TRANSITION AND BLOCK VALIDITY (go-ethereum / Geth).
    # Every Geth node on mainnet decodes a block, validates its header and body, runs
    # each transaction through the EVM and StateDB, and produces (stateRoot,
    # receiptsRoot, gasUsed, logsBloom) that must equal what the Ethereum spec and
    # every other client produce. Attacker input is a signed transaction, a deployed
    # contract's bytecode, a blob sidecar, or a block any permissionless builder or
    # proposer can craft. The files below sit on the path from those inputs to one of
    # four decisions: is the block valid, what state root results, how much gas and
    # ETH moved, and what is persisted as canonical. A question belongs here only if
    # it can be closed by an equality between the spec's result and Geth's result.
    # =================================================================================
    # -- core: block import, validation, processing, gas pool, genesis and chain config -
    # blockchain.go owns insertChain, ProcessBlock, reorg, SetCanonical, writeBlockWithState;
    # state_processor.go owns Process, ApplyTransactionWithEVM, the system-contract calls
    # (beacon root, parent hash, withdrawal / consolidation queues) and AssembleBlock.
    "core/block_validator.go",
    "core/blockchain.go",
    "core/blockchain_insert.go",
    "core/blockchain_reader.go",
    "core/error.go",
    "core/evm.go",
    "core/gaspool.go",
    "core/genesis.go",
    "core/genesis_alloc.go",
    "core/headerchain.go",
    "core/jumpdest.go",
    "core/sender_cacher.go",
    "core/state_prefetcher.go",
    "core/state_processor.go",
    "core/state_processor_parallel.go",
    "core/state_transition.go",
    "core/stateless.go",
    "core/types.go",
    "core/stateless/database.go",
    "core/stateless/encoding.go",
    "core/stateless/witness.go",

    # -- core/vm: interpreter, opcodes, gas tables, access lists, precompiles and cache --
    "core/vm/analysis_legacy.go",
    "core/vm/common.go",
    "core/vm/contract.go",
    "core/vm/contracts.go",
    "core/vm/eips.go",
    "core/vm/errors.go",
    "core/vm/evm.go",
    "core/vm/gas.go",
    "core/vm/gas_table.go",
    "core/vm/gascosts.go",
    "core/vm/instructions.go",
    "core/vm/interface.go",
    "core/vm/interpreter.go",
    "core/vm/jump_table.go",
    "core/vm/jump_table_export.go",
    "core/vm/jumpdests.go",
    "core/vm/memory.go",
    "core/vm/memory_table.go",
    "core/vm/opcodes.go",
    "core/vm/operations_acl.go",
    "core/vm/precompile_cache.go",
    "core/vm/stack.go",
    "core/vm/stack_table.go",

    # -- core/state: StateDB, journal, snapshots, access lists, transient storage, readers -
    "core/state/access_list.go",
    "core/state/database.go",
    "core/state/database_code.go",
    "core/state/database_history.go",
    "core/state/database_iterator.go",
    "core/state/database_mpt.go",
    "core/state/iterator.go",
    "core/state/journal.go",
    "core/state/reader.go",
    "core/state/reader_eip_7928.go",
    "core/state/reader_stater.go",
    "core/state/state_object.go",
    "core/state/statedb.go",
    "core/state/statedb_eip_7928.go",
    "core/state/statedb_hooked.go",
    "core/state/stateupdate.go",
    "core/state/transient_storage.go",
    "core/state/trie_prefetcher.go",
    "core/state/snapshot/context.go",
    "core/state/snapshot/conversion.go",
    "core/state/snapshot/difflayer.go",
    "core/state/snapshot/disklayer.go",
    "core/state/snapshot/generate.go",
    "core/state/snapshot/holdable_iterator.go",
    "core/state/snapshot/iterator.go",
    "core/state/snapshot/iterator_binary.go",
    "core/state/snapshot/iterator_fast.go",
    "core/state/snapshot/journal.go",
    "core/state/snapshot/snapshot.go",
    "core/state/snapshot/utils.go",

    # -- core/types: block, header, transactions, signing, sidecars, receipts, BAL --------
    "core/types/account.go",
    "core/types/block.go",
    "core/types/bloom9.go",
    "core/types/custody_bitmap.go",
    "core/types/deposit.go",
    "core/types/hashes.go",
    "core/types/hashing.go",
    "core/types/log.go",
    "core/types/receipt.go",
    "core/types/state_account.go",
    "core/types/transaction.go",
    "core/types/transaction_marshalling.go",
    "core/types/transaction_signing.go",
    "core/types/tx_access_list.go",
    "core/types/tx_blob.go",
    "core/types/tx_dynamic_fee.go",
    "core/types/tx_legacy.go",
    "core/types/tx_setcode.go",
    "core/types/withdrawal.go",
    "core/types/bal/bal.go",
    "core/types/bal/bal_encoding.go",
    "core/types/bal/bal_lookup.go",

    # -- core/txpool: transaction admission, nonce ordering, blob pool, delegation limits --
    "core/txpool/errors.go",
    "core/txpool/reserver.go",
    "core/txpool/subpool.go",
    "core/txpool/txpool.go",
    "core/txpool/validation.go",
    "core/txpool/txorder/ordering.go",
    "core/txpool/legacypool/legacypool.go",
    "core/txpool/legacypool/list.go",
    "core/txpool/legacypool/noncer.go",
    "core/txpool/legacypool/queue.go",
    "core/txpool/blobpool/blobpool.go",
    "core/txpool/blobpool/buffer.go",
    "core/txpool/blobpool/cache.go",
    "core/txpool/blobpool/config.go",
    "core/txpool/blobpool/conversion.go",
    "core/txpool/blobpool/evictheap.go",
    "core/txpool/blobpool/interface.go",
    "core/txpool/blobpool/limbo.go",
    "core/txpool/blobpool/lookup.go",
    "core/txpool/blobpool/priority.go",
    "core/txpool/blobpool/slotter.go",

    # -- core/rawdb: chain, state and receipt persistence, freezer, schema ----------------
    "core/rawdb/accessors_chain.go",
    "core/rawdb/accessors_history.go",
    "core/rawdb/accessors_indexes.go",
    "core/rawdb/accessors_metadata.go",
    "core/rawdb/accessors_snapshot.go",
    "core/rawdb/accessors_state.go",
    "core/rawdb/accessors_trie.go",
    "core/rawdb/ancient_scheme.go",
    "core/rawdb/ancient_utils.go",
    "core/rawdb/chain_freezer.go",
    "core/rawdb/chain_iterator.go",
    "core/rawdb/database.go",
    "core/rawdb/freezer.go",
    "core/rawdb/freezer_batch.go",
    "core/rawdb/freezer_meta.go",
    "core/rawdb/freezer_table.go",
    "core/rawdb/freezer_utils.go",
    "core/rawdb/schema.go",
    "core/rawdb/table.go",

    # -- consensus and params: header rules, base fee, blob gas, fork schedule, constants --
    "consensus/consensus.go",
    "consensus/errors.go",
    "consensus/beacon/consensus.go",
    "consensus/misc/dao.go",
    "consensus/misc/gaslimit.go",
    "consensus/misc/eip1559/eip1559.go",
    "consensus/misc/eip4844/eip4844.go",
    "params/config.go",
    "params/dao.go",
    "params/denomination.go",
    "params/network_params.go",
    "params/protocol_params.go",
    "params/forks/forks.go",

    # -- crypto: signature recovery, hashing, KZG, curve arithmetic behind precompiles ------
    "crypto/crypto.go",
    "crypto/keccak.go",
    "crypto/signature_cgo.go",
    "crypto/signature_nocgo.go",
    "crypto/secp256k1/curve.go",
    "crypto/secp256k1/scalar_mult_cgo.go",
    "crypto/secp256k1/scalar_mult_nocgo.go",
    "crypto/secp256k1/secp256.go",
    "crypto/secp256r1/verifier.go",
    "crypto/kzg4844/kzg4844.go",
    "crypto/kzg4844/kzg4844_ckzg_cgo.go",
    "crypto/kzg4844/kzg4844_ckzg_nocgo.go",
    "crypto/kzg4844/kzg4844_gokzg.go",
    "crypto/bn256/bn256_fast.go",
    "crypto/bn256/bn256_slow.go",
    "crypto/bn256/cloudflare/bn256.go",
    "crypto/bn256/cloudflare/curve.go",
    "crypto/bn256/cloudflare/gfp.go",
    "crypto/bn256/cloudflare/gfp2.go",
    "crypto/bn256/cloudflare/gfp6.go",
    "crypto/bn256/cloudflare/gfp12.go",
    "crypto/bn256/cloudflare/optate.go",
    "crypto/bn256/cloudflare/twist.go",
    "crypto/bn256/gnark/g1.go",
    "crypto/bn256/gnark/g2.go",
    "crypto/bn256/gnark/pairing.go",
    "crypto/blake2b/blake2b.go",
    "crypto/blake2b/blake2b_generic.go",
    "crypto/blake2b/blake2b_ref.go",
    "crypto/blake2b/blake2x.go",
    "crypto/keccak/hashes.go",
    "crypto/keccak/keccakf.go",
    "crypto/keccak/sha3.go",

    # -- rlp: the decoder every block, header, transaction and receipt passes through -----
    "rlp/decode.go",
    "rlp/encbuffer.go",
    "rlp/encode.go",
    "rlp/iterator.go",
    "rlp/raw.go",
    "rlp/typecache.go",

    # -- trie and triedb: state root computation, proofs, path-based persistence ----------
    "trie/bytepool.go",
    "trie/committer.go",
    "trie/encoding.go",
    "trie/errors.go",
    "trie/hasher.go",
    "trie/iterator.go",
    "trie/list_hasher.go",
    "trie/node.go",
    "trie/node_enc.go",
    "trie/proof.go",
    "trie/secure_trie.go",
    "trie/stacktrie.go",
    "trie/stacktrie_partial.go",
    "trie/tracer.go",
    "trie/trie.go",
    "trie/trie_id.go",
    "trie/trie_reader.go",
    "trie/trienode/node.go",
    "trie/trienode/proof.go",
    "triedb/database.go",
    "triedb/history.go",
    "triedb/preimages.go",
    "triedb/states.go",
    "triedb/database/database.go",
    "triedb/hashdb/database.go",
    "triedb/pathdb/buffer.go",
    "triedb/pathdb/context.go",
    "triedb/pathdb/database.go",
    "triedb/pathdb/difflayer.go",
    "triedb/pathdb/disklayer.go",
    "triedb/pathdb/errors.go",
    "triedb/pathdb/execute.go",
    "triedb/pathdb/flush.go",
    "triedb/pathdb/history.go",
    "triedb/pathdb/history_reader.go",
    "triedb/pathdb/history_state.go",
    "triedb/pathdb/history_trienode.go",
    "triedb/pathdb/iterator.go",
    "triedb/pathdb/journal.go",
    "triedb/pathdb/layertree.go",
    "triedb/pathdb/lookup.go",
    "triedb/pathdb/nodes.go",
    "triedb/pathdb/reader.go",
    "triedb/pathdb/states.go",
    "triedb/pathdb/verifier.go",

    # -- engine API and block building: where a builder's payload enters and leaves Geth --
    "beacon/engine/bapl_encode.go",
    "beacon/engine/epe_encode.go",
    "beacon/engine/errors.go",
    "beacon/engine/types.go",
    "eth/catalyst/api.go",
    "eth/catalyst/queue.go",
    "miner/miner.go",
    "miner/payload_building.go",
    "miner/pending.go",
    "miner/worker.go",

    # =================================================================================
    # NOT AUDITED (excluded from every variant): every *_test.go, tests/, fuzzers and
    # testing helpers (chain_makers.go, api_testing.go, dbtest, ancienttest, testrand,
    # testlog); generated code (gen_*.go, *_generated.go, *.pb.go, gencodec outputs
    # ed_codec.go / epe_decode.go / pa_codec.go) and mkalloc.go; metrics.go files, log,
    # event and telemetry; p2p/, eth/protocols/, eth/downloader/, eth/fetcher/ and
    # discovery (malicious-peer surface); rpc/, internal/ethapi/, graphql/, eth/filters,
    # eth/tracers, ethclient/ and console (JSON-RPC surface); cmd/, accounts/, signer/,
    # node/, ethdb/ drivers; pre-merge engines (ethash, clique) and pre-activation code
    # (verkle, bintrie, transitiontrie, overlay, database_ubt); go.mod, Makefile, docs
    # and README. A defect in any of these is only in scope when it is reachable from
    # the audited code above through a transaction or block executed on mainnet.
    # =================================================================================
]


target_scopes = [
    "Critical. THE OPCODE RESULT GETH COMPUTES MUST EQUAL THE SPEC'S. `EVMInterpreter.Run` dispatches through `JumpTable` entries whose `execute`, `constantGas`, `dynamicGas`, `memorySize` and stack bounds are assembled by `newOsakaInstructionSet` and mutated in place by `enable1153`, `enable5656`, `enable6780`, `enable7702`, `enable7939`, `enable7843`, `enable8024`; `opExtCodeCopy`, `opExtCodeHash` and `opExtCodeSize` read through `EVM.resolveCode` / `resolveCodeHash` for EIP-7702 delegations; `opSelfdestruct` and `enable6780` only send balance unless `CreateContract` marked the account new in this tx; `opBlobHash`, `opMcopy`, `opTload`, `opTstore` and `opBlockhash` guard indices; `gasSStoreEIP2200`, `makeGasSStoreFunc`, `gasCallEIP2929`, `gasExtCodeCopyEIP2929` and `makeCallVariantGasCallEIP7702` compute warm/cold and delegation surcharges; `Memory.Resize` and `calcMemSize64` bound expansion. Probe every place a contract any user can deploy reads or writes a value other clients would not: an EXTCODE* on a delegated account returning the target's code instead of the 23-byte designator; a SELFDESTRUCT in a CREATE2-redeployed address in the same tx; an SSTORE refund path where original, current and new values collide; a MCOPY with overlapping src/dst and length 0; a CLZ or shift on a 256-bit edge value; a gas constant that differs from `params` for one fork. Identity: (stack, memory, storage, gasUsed, halt reason) after each opcode == the EIP-defined result, so every client computes the same stateRoot.",

    "Critical. THE PRECOMPILE OUTPUT AND GAS MUST EQUAL THE SPEC FOR EVERY INPUT LENGTH. `RunPrecompiledContract` charges `RequiredGas` then calls `Run`, consulting `PrecompileCache.load` / `store` with the key from `precompileCacheKey` built from each contract's `NormalizeInput` (`normalizeZeroPadded` strips trailing zero bytes) and `Cacheable`; `bigModExp.RequiredGas` implements EIP-7883 limits and `Run` falls back for base or modulus of length 0; `ecrecover.Run` validates `v`, `r`, `s` bounds; `bls12381G1MultiExp`, `bls12381G2MultiExp`, `bls12381Pairing`, `decodePointG1`, `decodePointG2` enforce subgroup and encoding; `kzgPointEvaluation.Run` checks `versionedHash`, field canonicality and `VerifyProof`; `p256Verify.Run` wraps `secp256r1.Verify`; `blake2F.Run` reads the final flag byte; `bn256PairingIstanbul` uses `newCurvePoint` / `newTwistPoint`. Show a call any contract can make where Geth returns a result, success flag or gas that differs from the EIP: two distinct inputs whose normalized cache key collides and return a stale output; a padded input accepted where the spec requires exact length; an out-of-range field element or non-canonical point accepted; a modexp with oversized exponent charged less than specified; a p256 signature with r or s at the group order. Identity: (output, success, gasUsed) of the precompile == the spec's function applied to the raw input, independent of cache state.",

    "Critical. A SET-CODE AUTHORIZATION MUST ONLY CHANGE THE CODE OF THE ACCOUNT THAT SIGNED IT, ONCE, IN THE RIGHT ORDER. `stateTransition.applyAuthorizations` iterates `SetCodeAuthorization` entries through `validateAuthorization` (`Authority` via `SigHash` and `crypto.Ecrecover`, chain id 0 or current, nonce equals account nonce, code empty or a delegation) and `applyAuthorization` (`AddressToDelegation`, `SetCode`, `SetNonce`, refund via `params.CallNewAccountGas - params.TxAuthTupleGas`, the `authorities` map for duplicate authorities); `ParseDelegation` recognises the `0xef0100` prefix; `EVM.resolveCode` follows one hop; `preCheck` rejects a sender with non-delegation code (EIP-3607) and `TransactionToMessage` requires a non-nil `To` and non-empty `AuthList`; `LegacyPool.validateAuth`, `checkDelegationLimit` and `lookup.addAuthorities` gate the pool. Show a type-4 transaction any user can sign that leaves an account's code, nonce or balance different from the spec: a duplicate authority whose second tuple is applied with the wrong nonce; an authority whose `s` is above half the curve order or `v` above 1 still recovered; a delegation to a precompile or to another delegated account resolved two hops; a refund granted for an authority that already had code; a sender that delegates to itself and then executes; an authorization applied before `buyGas` fails so state is mutated by an invalid tx. Identity: after the tx, `GetCode(authority)` and `GetNonce(authority)` for every tuple == the EIP-7702 result, and no other account's code changed.",

    "Critical. GAS AND ETH MUST BE CONSERVED EXACTLY THROUGH BUYGAS, EXECUTION AND SETTLEMENT. `TransactionToMessage` computes `GasPrice` from `GasFeeCap`, `GasTipCap` and `baseFee`; `IntrinsicGas` and `FloorDataGas` (EIP-7623 tokens, EIP-3860 init-code words, access list and auth tuples) feed `buyGas` which charges `gas * gasPrice + blobGas * blobFee` and `initRuntimeGasBudget`; `preCheck` enforces nonce, EIP-3607, fee caps, `MaxFeePerBlobGas` and blob hash versions; `execute` routes to `executeCreate` or `executeCall`, `chargeCallRecipientEIP2780`; `settleGas` applies `calcRefund` (quotient 5 post-3529), the floor, and pays `effectiveTip` to `Coinbase`; `GasPool.SubGas` bounds the block; `MakeReceipt` records `CumulativeGasUsed`, `Status` and logs; `ApplyTransactionWithEVM` sets `blobGasUsed`. Show a transaction any account can send where the sender's balance, the coinbase's balance and the burned base fee do not sum to the pre-state: a refund exceeding gasUsed/5; a floor gas applied after refund instead of before; a create with `To == nil` and value where the contract address already holds balance; an overflow in `gas * gasPrice` or in the blob fee multiplication; a failed tx that still credits the coinbase or mutates a nonce twice; a `CumulativeGasUsed` in the receipt that differs from `GasPool` accounting so `receiptsRoot` diverges. Identity: sum of all balances after == sum before plus coinbase tip minus burned base and blob fees, and `header.GasUsed` == the sum of receipts' gas the spec defines.",

    "Critical. THE BLOCK GETH ACCEPTS MUST BE EXACTLY THE BLOCK THE SPEC ACCEPTS. `ConsensusAPI.newPayload` converts through `engine.ExecutableDataToBlock` (`DecodeTransactions`, `attachAccessList`, `validateRequests` ordering and requestsHash, blob hashes versus `versionedHashes`, `parentBeaconBlockRoot`) then `InsertBlockWithoutSetHead`; `Beacon.verifyHeader` checks difficulty, nonce, uncles, timestamp, `gasLimit` via `VerifyGaslimit`, `extra`, `withdrawalsHash`, `VerifyEIP1559Header` with `CalcBaseFee`, `VerifyEIP4844Header` with `CalcExcessBlobGas` (Osaka / BPO1 / BPO2 schedules via `latestBlobConfig`) and `parentBeaconRoot`; `BlockValidator.ValidateBody` checks `txHash`, `uncleHash`, `withdrawalsHash`, `blobGasUsed`, blob count against `MaxBlobsPerBlock` and `MaxBlobGasPerBlock`; `ValidateState` compares `Bloom`, `ReceiptHash`, `Root` and `requestsHash`; `StateProcessor.Process` runs `ProcessBeaconBlockRoot`, `ProcessParentBlockHash`, then `ProcessWithdrawalQueue`, `ProcessConsolidationQueue`, `ParseDepositLogs` through `processRequestsSystemCall` with `systemCallGasBudget`; `Beacon.Finalize` applies `Withdrawal` amounts in Gwei. Show a block any builder or proposer can construct that Geth accepts and another client rejects, or the reverse: a blob count valid under BPO1 but checked against the Cancun config at a fork boundary timestamp; an `excessBlobGas` computed with the pre-Osaka formula after Osaka; a requests list with an empty type or unsorted types that still hashes right; a deposit log with a malformed length that `ParseDepositLogs` skips instead of failing; a withdrawal amount overflow in Gwei-to-Wei; a system call that reverts and is silently ignored. Identity: `InsertChain` accepts block B if and only if the execution spec accepts B, and the resulting `stateRoot` is equal.",

    "Critical. THE STATE ROOT MUST REFLECT EXACTLY THE JOURNALED CHANGES. `StateDB.Snapshot` / `RevertToSnapshot` walk `journal.revertToSnapshot` over entries such as `createObjectChange`, `createContractChange`, `selfDestructChange`, `balanceChange`, `nonceChange`, `storageChange`, `codeChange`, `transientStorageChange`, `accessListAddAccountChange`, `accessListAddSlotChange`; `Finalise` deletes empty (EIP-161) and self-destructed objects, resets `transientStorage` per tx and, on Amsterdam, emits the `bal.ConstructionBlockAccessList`; `IntermediateRoot` and `Commit` push dirty objects through `triePrefetcher` and `stateUpdate`; `CreateContract` marks `newContract` used by `SelfDestruct6780`; `Prepare` builds the EIP-2929 warm set from sender, coinbase, `dst`, precompiles and the access list; `StateDB.Copy` duplicates journal state for `ProcessParallel`; `hookedStateDB` wraps every mutation. Show a transaction sequence any user can submit where the root Geth commits differs from the spec: a revert that restores a self-destructed object but not its `newContract` flag; a storage slot written, reverted and written again whose `originStorage` no longer matches disk; an empty account touched then credited zero that is deleted in one path and kept in another; a transient slot surviving into the next transaction; a journal entry whose `revert` is a no-op for the field that changed; a parallel prefetch that commits a stale `stateObject`. Identity: `IntermediateRoot` after tx i == the root of applying the spec's state changes for txs 0..i to the parent root, in both the sequential and parallel processors.",

    "Critical. THE TRANSACTION GETH DECODES AND ATTRIBUTES MUST BE THE ONE THAT WAS SIGNED. `Transaction.UnmarshalBinary` / `decodeTyped` dispatch on the first byte to `BlobTx.decode` (`blobTxWithBlobsV0` / `blobTxWithBlobsV1` sidecar forms, `BlobTxSidecar.ToV1`, `ValidateBlobCommitmentHashes`, `CellProofsAt`), `SetCodeTx.decode`, `DynamicFeeTx`, `AccessListTx` and `LegacyTx`; `rlp.Decode` enforces canonical integers and list lengths; `Sender` caches by `Signer` and calls `modernSigner.Sender` or `EIP155Signer.Sender` into `recoverPlain` (`crypto.ValidateSignatureValues`, homestead `s` bound, `Ecrecover`) with `deriveChainId`; `MakeSigner` picks the signer by fork; `Transaction.Hash` and `sigHash` encode the fields the signer covers; `kzg4844.CalcBlobHashV1` and `IsValidVersionedHash` bind sidecars to `BlobHashes`; `txpool.ValidateCells` / `validateCellsOsaka` and `blobpool.conversionQueue.convert` migrate V0 proofs to cell proofs; `GetBlobsV4` serves cells by `CustodyBitmap`. Show a byte string any user can broadcast that Geth attributes, hashes or includes differently from the spec: two encodings recovering different senders for one hash; a legacy tx with `v` encoding a chain id whose parity is misread; a blob tx whose commitments match `BlobHashes` but whose cell proofs verify against another blob; a sidecar V1 with proof count not equal to `CellsPerBlob * blobs` that `ToV1` accepts; a setcode tx with an empty auth list decoded as valid; a non-canonical RLP integer that the decoder accepts but the spec rejects. Identity: (sender, hash, fields, blobs) Geth derives from bytes B == the (sender, hash, fields, blobs) every other client derives from B.",

    "High. EVERY TRANSACTION THE POOL PROMOTES MUST BE EXECUTABLE, AND EVERY BLOCK GETH BUILDS MUST BE VALID. `txpool.ValidateTransaction` checks type, size, gas limit against `head.GasLimit`, fee caps, `IntrinsicGas`, `FloorDataGas`, blob count and `validateBlobSidecar`; `ValidateTransactionWithState` checks nonce, cost against `ExistingBalance` and `FirstNonceGap`; `LegacyPool.add`, `enqueueTx`, `promoteTx`, `promoteExecutables`, `demoteUnexecutables`, `truncatePending` and `reset` maintain per-account `list` ordering with `noncer`; `BlobPool.addLocked`, `recheck`, `reorg`, `reinject`, `limbo` and `evictGapped` handle inclusion and reorgs; `checkDelegationLimit` and `HasPendingAuth` gate accounts with delegations; `Miner.fillTransactions`, `commitTransactions`, `commitBlobTransaction`, `txFitsSize` and `applyTransaction` assemble the payload that `Payload.Resolve` hands to the proposer through `GetPayloadV5`. Show a transaction any user can send that makes every Geth proposer build a block other clients reject, or that sits in pending while being unexecutable so ordinary users' transactions are excluded: a blob tx promoted whose sidecar was converted to a proof version the block cannot carry; a nonce list where a replacement lowers the cost below balance for later txs still marked pending; a delegated account's second tx admitted past `checkDelegationLimit` via the blob pool; a builder including a tx whose `FloorDataGas` exceeds the block's remaining gas after `GasPool` accounting; a reorg that reinjects a tx already included on the new head. Identity: the set of txs in `Pending` == the set executable on the head state, and every block `buildPayload` returns passes `ValidateBody` and `ValidateState` on every client.",

    "High. THE CANONICAL CHAIN AND STATE GETH PERSISTS MUST EQUAL WHAT IT EXECUTED. `BlockChain.writeBlockWithState` writes block, receipts and `statedb.Commit` through `triedb.Update` into `pathdb.layerTree.add` / `diffLayer` / `diskLayer.commit`, with `buffer.flush`, `journal` and `history` for reverts; `reorg` collects deleted and added chains, rewrites `rawdb.WriteCanonicalHash`, `WriteTxLookupEntries`, deletes stale lookups and emits removed logs; `SetCanonical`, `insertSideChain` and `recoverAncestors` handle blocks whose parent state is missing; `setHeadBeyondRoot`, `rewindPathHead` and `rewindHashHead` roll back on restart; `rawdb.chainFreezer.freeze` moves finalized blocks into `freezerTable` with `freezerBatch` indexes; `snapshot.Tree.Update`, `diffLayer.flatten`, `journal` and `generate` mirror state for fast reads; `pathdb.Recover` replays `history`. Show a valid block or reorg sequence any builder can cause on mainnet after which a Geth node answers a different head, state or receipt than it executed, or cannot follow the chain without manual intervention: a reorg deeper than the diff-layer window that rewinds to a root the snapshot no longer has; a side-chain insert that writes canonical hashes for blocks never executed; a freezer batch written with an index ahead of its data; a tx lookup left pointing at a reorged block; a pathdb journal loaded after a crash whose top layer root differs from the written head. Identity: (canonical hash, state root, receipts) stored for height h == (hash, root, receipts) produced by the last `ProcessBlock` that set h canonical.",

    "Critical. THE MISSING INVARIANT - what nobody built. No check ties the `PrecompileCache` output back to the raw input once `NormalizeInput` accepted it; nothing asserts `ValidateState`'s root comparison ran on the same `StateDB` that `ProcessParallel` committed; the fork-schedule predicates (`IsOsaka`, `IsBPO1`, `IsBPO2`, `IsAmsterdam`) are read separately by the blob config, precompile set, jump table and signer with no single rule set proving they agree at a boundary timestamp; system-contract calls in `processRequestsSystemCall` treat a revert or empty code as a soft error; the `blobpool` conversion path trusts proof counts it did not verify; `ExecutableDataToBlock` reconstructs a header from fields the CL supplied and only later checks the hash. Identify the FIRST place one of these unstated equalities is violated by an unprivileged party through a transaction, deployed contract, blob sidecar or permissionless block, prove it with a Go test (`core.GenerateChain`, `state_processor` fixtures or an execution-spec state test) that asserts both sides (spec result versus Geth result, ETH before versus after, accepted set versus spec set, persisted versus executed) and show that no later step in `insertChain` can detect or reverse it.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate consensus / state-transition / block-validity audit questions for one go-ethereum target.

    ```
    target_file format:
    "'File Name: core/vm/instructions.go -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate execution-client consensus security audit questions for this exact
    go-ethereum target:

    {target_file}

    Project focus:
    Geth is the majority Ethereum execution client. Every node decodes a block,
    verifies its header and body, runs each transaction through the EVM and StateDB,
    applies system contracts and withdrawals, and produces (stateRoot, receiptsRoot,
    gasUsed, logsBloom) that must equal what the execution spec and every other client
    produce. Untrusted input enters as a signed transaction of any type, the bytecode
    of a contract anyone can deploy, a blob sidecar, or a block any permissionless
    builder or proposer submits. The system decides (a) whether the block is valid;
    (b) what state root, receipts and gas result; (c) how much ETH moved and where;
    (d) what is persisted as canonical. Anything Geth accepts, computes, charges or
    stores that the spec does not is the bug.

    Rules:
    * Treat `File Name:` as the exact file.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Go symbols (exported function, method, constant, opcode, error var,
      struct field) as they appear in the file.
    * EVERY question must close on an equality that must hold across a call. State it
      explicitly. Narrative questions with no stated equality are rejected.
    * Attacker is unprivileged only: an ordinary mainnet account with its own ETH and
      keys, a contract deployer, a blob-transaction sender, or a permissionless block
      builder or proposer whose block every node executes. They may send any
      transaction and order their own transactions and contract calls.
    * Attacker is NOT a malicious peer, node, RPC client or Engine API caller, not the
      node operator, not a majority of validators, and holds no leaked key. No
      malicious peer or devp2p message; no compromised dependency or machine; no
      social engineering.
    * PROGRAM EXCLUSIONS - a question landing in any of these wastes the whole batch:
      - Tests, fuzzers, generated code (gen_*.go, *_generated.go, *.pb.go), cmd/,
        docs and build files are OUT OF SCOPE.
      - Denial of service, resource exhaustion, unbounded memory or disk growth, slow
        paths, rate limiting and timeouts are OUT OF SCOPE. A deterministic panic or
        halt from one valid transaction or block is IN scope.
      - Anything reachable only through a publicly exposed JSON-RPC, GraphQL, Beacon
        or Engine API, or through p2p/devp2p/discovery messages, is OUT OF SCOPE.
      - Pre-merge engines (ethash, clique) and code not activated on mainnet (verkle,
        binary trie, overlay transition) are OUT OF SCOPE; Amsterdam-gated code is High
        at most unless it also changes pre-Amsterdam execution.
      - Also excluded: publicly known or already fixed issues, leaked keys, 51% or
        economic attacks, centralization risk, best-practice notes, feature requests,
        spec ambiguities with no client divergence, and theoretical findings.
    * IN-SCOPE IMPACTS - every question must land on one and name it:
      Critical: a consensus split where Geth accepts or rejects a mainnet block other
      clients treat oppositely, or commits a different stateRoot; ETH created, stolen
      or burned from an account the attacker does not control; a single transaction
      or block that crashes or halts every Geth node.
      High: every Geth proposer building an invalid block from one transaction; state
      or chain persistence that no longer matches what was executed after a valid
      block or reorg; a gas or receipt divergence that only surfaces on a rare path.
    * Every question must be a concrete real-world scenario an unprivileged party can
      trigger with a transaction, deployed contract, blob or block on mainnet rules.
    * A returned error is a finding only when it rejects a spec-valid block or accepts
      a spec-invalid one - say which.
    * Generate 40 to 80 high-signal questions.
    * At least 70% must land on a Critical impact rather than a High one.
    * Every question must be testable locally with a Go test (`core.GenerateChain`, a
      `StateProcessor` fixture, `vm/runtime`, or an execution-spec state test) on a
      private chain. Never propose testing on mainnet or a public testnet.
    * Avoid generic checklist questions and repeated root causes.
    * Prefer questions that name TWO values that must be equal and ask whether they are:
      Geth result and spec result, ETH before and after, gas charged and gas specified,
      block accepted and block spec-valid, state persisted and state executed.

    Known dead ends - do NOT generate questions about these:
    * Anything needing a malicious peer, node operator, RPC caller, CL client or
      validator majority.
    * DoS, memory, disk, logging, or a user harming only their own balance.
    * Code paths not active on mainnet through the current fork schedule.
    * Findings only reproducible through tests or tooling.

    Core equalities (each question must close on one):
    * SPEC PARITY: (stateRoot, receiptsRoot, gasUsed, logs) Geth computes == the spec's.
    * VALIDITY TRUTH: the set of blocks and txs Geth accepts == the set the spec accepts.
    * ETH CONSERVATION: balances after == balances before + issuance - burn, exactly.
    * GAS TRUTH: gas charged and refunded == gas the EIPs define for that input.
    * CODE TRUTH: code, nonce and storage changed == accounts the tx authorised.
    * PERSISTENCE TRUTH: (head, root, receipts) stored == (head, root, receipts) executed.

    Each question must include:
    1. target exported function, method, opcode or constant;
    2. attacker input (the concrete transaction fields, bytecode, blob, authorization
       or block fields that matter);
    3. preconditions (fork, account state, prior txs in the block, cache state);
    4. call sequence through block import, state transition, EVM and StateDB;
    5. the equality that breaks, written explicitly;
    6. scoped impact and which nodes or accounts are affected;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Method: function_name] Can an unprivileged ATTACKER_INPUT under PRECONDITIONS trigger CALL_SEQUENCE, breaking the equality EQUALITY, causing scoped impact: SCOPE_IMPACT against PARTY? Proof idea: Go test PARAMETERS asserting SPEC_PARITY, VALIDITY_TRUTH, ETH_CONSERVATION, GAS_TRUTH, CODE_TRUTH, or PERSISTENCE_TRUTH.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a consensus / state-transition exploit-validation prompt for go-ethereum.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: an ordinary mainnet account with its own ETH and keys, a contract deployer, a blob-transaction sender, or a permissionless block builder or proposer whose block every node executes. They may send any transaction and order their own calls.
- Reject anything requiring a malicious peer, node, RPC or Engine API caller, the node operator, a validator majority, a leaked key, a compromised dependency or machine, or social engineering.
- OUT OF SCOPE, reject on sight: tests, fuzzers, generated code (gen_*.go, *_generated.go, *.pb.go), cmd/, docs, build files; denial of service, resource exhaustion, unbounded memory or disk growth, slow paths, rate limiting, timeouts; anything reachable only through exposed JSON-RPC, GraphQL, Beacon or Engine API or p2p messages; pre-merge engines and code not activated on mainnet (verkle, binary trie, overlay); publicly known or fixed issues; 51% or economic attacks; centralization risk; best-practice notes; theoretical findings. A deterministic panic or halt from one valid transaction or block is IN scope.
- The impact must be one of: Critical - a consensus split where Geth accepts or rejects a mainnet block other clients treat oppositely or commits a different stateRoot, ETH created, stolen or burned from an account the attacker does not control, a single transaction or block that crashes or halts every Geth node; High - every Geth proposer building an invalid block from one transaction, persistence that no longer matches execution after a valid block or reorg, a gas or receipt divergence on a rare path.
- Focus on real impact: something Geth accepts, computes, charges or stores that the spec does not.

## Validate
- Write the equality the question claims is broken between two named values BEFORE tracing any code.
- Trace the exact reachable path from the attacker's transaction, bytecode, blob or block and record every read and write of `gas` / `GasBudget` / refund, balance, nonce, code, storage and transient slots, `Root` / `ReceiptHash` / `Bloom` / `GasUsed`, `excessBlobGas` / `baseFee`, and the canonical hash or layer written.
- Evaluate both sides of the equality before and after against the EIP text. If they still match, output no vulnerability.
- Check whether `verifyHeader`, `ValidateBody`, `ValidateState`, `preCheck`, `IntrinsicGas` / `FloorDataGas`, `validateAuthorization`, `ValidateSignatureValues`, `RequiredGas`, `Prepare`, `Finalise`, the journal revert, or the execution-spec tests in tests/ already prevent the divergence.
- State what the attacker gains per transaction or block and whether it is repeatable.
- Require exact file/function support and a reproducible Go test on a private chain.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[The broken equality, the code path, root cause, the attacker's exact input, exploit flow, and why existing guards fail]

### Impact Explanation
[What is accepted, computed, charged or stored wrongly, which nodes or accounts, repeatability, matching severity category]

### Likelihood Explanation
[Preconditions, fork and state required, attacker cost, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Go test plan with the exact assertions on both sides of the equality]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for go-ethereum claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- A claim is only valid if the report states the broken equality between two named values and shows both sides concretely. Reject prose-only claims.
- Reject anything requiring a malicious peer, node, RPC or Engine API caller, the node operator, a validator majority, a leaked key, a compromised dependency or machine, or social engineering.
- OUT OF SCOPE, reject on sight: tests, fuzzers, generated code (gen_*.go, *_generated.go, *.pb.go), cmd/, docs, build files; denial of service, resource exhaustion, unbounded memory or disk growth, slow paths, rate limiting, timeouts; anything reachable only through exposed JSON-RPC, GraphQL, Beacon or Engine API or p2p messages; pre-merge engines and code not activated on mainnet (verkle, binary trie, overlay); publicly known or fixed issues; 51% or economic attacks; centralization risk; best-practice notes; feature requests; theoretical findings. A deterministic panic or halt from one valid transaction or block is IN scope.
- The impact must be one of: Critical - a consensus split where Geth accepts or rejects a mainnet block other clients treat oppositely or commits a different stateRoot, ETH created, stolen or burned from an account the attacker does not control, a single transaction or block that crashes or halts every Geth node; High - every Geth proposer building an invalid block from one transaction, persistence that no longer matches execution after a valid block or reorg, a gas or receipt divergence on a rare path.
- Reject claims where the only loss is the attacker's own balance or gas.
- Reject if the bug was already fixed, publicly disclosed, or covered by a known-issues list.
- A valid report must be triggerable by an unprivileged party against the current code with a transaction, deployed contract, blob or block under mainnet fork rules.
- A PoC is mandatory. Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function/method/opcode/constant, and line references.
2. The equality written explicitly, with both sides shown before and after, citing the EIP or spec text.
3. Clear root cause: which opcode or precompile semantic, gas or refund rule, signature or decoding rule, header or body check, journal or persistence step causes it.
4. Reachable exploit path: preconditions -> attacker transaction or block -> block import, state transition, EVM and StateDB sequence -> observed divergence.
5. `verifyHeader`, `ValidateBody`, `ValidateState`, `preCheck`, `IntrinsicGas` / `FloorDataGas`, `validateAuthorization`, `ValidateSignatureValues`, `RequiredGas`, `Prepare`, `Finalise`, the journal revert and the execution-spec tests reviewed and shown insufficient.
6. Impact stated concretely: which nodes split, which accounts lose or gain, and whether it is repeatable.
7. Reproducible proof: Go test on a private chain with the asserted values.

## Silent Triage Questions
Before output, internally answer:
- What exactly is the equality, and does it actually fail against the spec?
- Can an ordinary account, contract or permissionless builder trigger it with no privileged role and no peer-level access?
- Is the flaw in this repo's code, not in the spec, the CL client or a dependency?
- What is accepted, computed, charged or stored wrongly, who is affected, and can it be repeated?
- Would the Ethereum Foundation bug bounty panel accept the exploit path for Geth?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the broken equality and impact]

## Finding Description
[Exact code path, the equality, root cause, exploit flow, and why existing guards fail]

## Impact Explanation
[What is accepted, computed, charged or stored wrongly, affected nodes or accounts, repeatability, severity category]

## Likelihood Explanation
[Attacker capability, preconditions, fork and state required, cost, feasibility]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or Go test plan with concrete assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for go-ethereum.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope repo context only (`core/**`, `consensus/**`, `params/**`, `crypto/**`, `rlp/**`, `trie/**`, `triedb/**`, `beacon/engine/**`, `eth/catalyst/**`, `miner/**`, excluding tests, fuzzers, generated files, metrics and pre-activation code). Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only unprivileged analogs that break an equality: a block or transaction Geth accepts that the spec rejects or the reverse, a stateRoot or receipt that differs from the spec, ETH created or moved without authorisation, gas charged that differs from the EIPs, code or nonce changed on an account that did not authorise it, or a persisted head or state that differs from what was executed.
- OUT OF SCOPE, reject on sight: tests, fuzzers, generated code, cmd/, docs, build files; denial of service, resource exhaustion, unbounded memory or disk growth, slow paths, rate limiting, timeouts; anything reachable only through exposed JSON-RPC, GraphQL, Beacon or Engine API; malicious peer, node or devp2p assumptions; pre-merge engines and code not activated on mainnet; publicly known or fixed issues; 51% or economic attacks; centralization risk; best-practice notes; theoretical findings. A deterministic panic or halt from one valid transaction or block is IN scope.
- The impact must be one of: Critical - a consensus split where Geth accepts or rejects a mainnet block other clients treat oppositely or commits a different stateRoot, ETH created, stolen or burned from an account the attacker does not control, a single transaction or block that crashes or halts every Geth node; High - every Geth proposer building an invalid block from one transaction, persistence that no longer matches execution after a valid block or reorg, a gas or receipt divergence on a rare path.
- Reject analogs where the only loss is the attacker's own balance or gas.

## Validate
- Map the bug class to the strongest reachable path in this repo and state the equality it would break.
- Evaluate both sides before and after the attacker's transaction or block against the EIP text.
- Prove root cause with exact file/function support.
- Accept only concrete consensus divergence, unauthorised ETH or code change, wrong gas, network-wide crash, invalid block production, or persistence mismatch.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
