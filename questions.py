import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'stacks-network/stacks-core'
# todo: the name of the repository
REPO_NAME = 'stacks-core'

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
    # LENS: THE NETWORK BOUNDARY (P2P, RPC, STACKERDB, ATLAS).
    # A node's open ports accept bytes from anyone. The files below sit on the path from
    # an unauthenticated remote message - a P2P handshake, a gossiped block or tx, an
    # HTTP request, a StackerDB chunk, an Atlas attachment - to one of three decisions:
    # is this peer who it claims and allowed to say this, does the node's stored or
    # relayed state match what was actually authorized, and does the handler stay within
    # its resource and trust bounds. A question belongs here only if it closes on an
    # equality between what a remote party authenticated and what the node stored,
    # relayed or served - or a remotely reachable memory/panic fault with a named impact.
    # =================================================================================
    # -- The P2P protocol: framing, handshake, and the chat state machine ---------------

    # -- clarity-types: Clarity value, type and effect model -------------------------------
    "clarity-types/src/effects/asset_map.rs",
    "clarity-types/src/effects/mod.rs",
    "clarity-types/src/errors/mod.rs",
    "clarity-types/src/lib.rs",
    "clarity-types/src/representations.rs",
    "clarity-types/src/types/mod.rs",
    "clarity-types/src/types/serialization.rs",
    "clarity-types/src/types/signatures.rs",
    "clarity-types/src/version.rs",

    # -- clarity: the Clarity language, analyser, interpreter, costs and database ----------
    "clarity/src/libclarity.rs",
    "clarity/src/vm/analysis/analysis_db.rs",
    "clarity/src/vm/analysis/arithmetic_checker/mod.rs",
    "clarity/src/vm/analysis/contract_interface_builder/mod.rs",
    "clarity/src/vm/analysis/errors.rs",
    "clarity/src/vm/analysis/mod.rs",
    "clarity/src/vm/analysis/read_only_checker/mod.rs",
    "clarity/src/vm/analysis/trait_checker/mod.rs",
    "clarity/src/vm/analysis/type_checker/contexts.rs",
    "clarity/src/vm/analysis/type_checker/mod.rs",
    "clarity/src/vm/analysis/type_checker/v2_05/contexts.rs",
    "clarity/src/vm/analysis/type_checker/v2_05/mod.rs",
    "clarity/src/vm/analysis/type_checker/v2_05/natives/assets.rs",
    "clarity/src/vm/analysis/type_checker/v2_05/natives/maps.rs",
    "clarity/src/vm/analysis/type_checker/v2_05/natives/mod.rs",
    "clarity/src/vm/analysis/type_checker/v2_05/natives/options.rs",
    "clarity/src/vm/analysis/type_checker/v2_05/natives/sequences.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/contexts.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/mod.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/natives/assets.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/natives/conversions.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/natives/maps.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/natives/mod.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/natives/options.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/natives/post_conditions.rs",
    "clarity/src/vm/analysis/type_checker/v2_1/natives/sequences.rs",
    "clarity/src/vm/analysis/types.rs",
    "clarity/src/vm/ast/definition_sorter/mod.rs",
    "clarity/src/vm/ast/errors.rs",
    "clarity/src/vm/ast/expression_identifier/mod.rs",
    "clarity/src/vm/ast/mod.rs",
    "clarity/src/vm/ast/parser/mod.rs",
    "clarity/src/vm/ast/parser/v1.rs",
    "clarity/src/vm/ast/parser/v2/lexer/error.rs",
    "clarity/src/vm/ast/parser/v2/lexer/mod.rs",
    "clarity/src/vm/ast/parser/v2/lexer/token.rs",
    "clarity/src/vm/ast/parser/v2/mod.rs",
    "clarity/src/vm/ast/stack_depth_checker.rs",
    "clarity/src/vm/ast/sugar_expander/mod.rs",
    "clarity/src/vm/ast/traits_resolver/mod.rs",
    "clarity/src/vm/ast/types.rs",
    "clarity/src/vm/callables.rs",
    "clarity/src/vm/clarity.rs",
    "clarity/src/vm/contexts.rs",
    "clarity/src/vm/contracts.rs",
    "clarity/src/vm/costs/constants.rs",
    "clarity/src/vm/costs/cost_functions.rs",
    "clarity/src/vm/costs/costs_1.rs",
    "clarity/src/vm/costs/costs_2.rs",
    "clarity/src/vm/costs/costs_2_testnet.rs",
    "clarity/src/vm/costs/costs_3.rs",
    "clarity/src/vm/costs/costs_4.rs",
    "clarity/src/vm/costs/costs_5.rs",
    "clarity/src/vm/costs/errors.rs",
    "clarity/src/vm/costs/execution_cost.rs",
    "clarity/src/vm/costs/mod.rs",
    "clarity/src/vm/database/caching/mod.rs",
    "clarity/src/vm/database/caching/weight_limited_fifo.rs",
    "clarity/src/vm/database/clarity_db.rs",
    "clarity/src/vm/database/clarity_store.rs",
    "clarity/src/vm/database/key_value_wrapper.rs",
    "clarity/src/vm/database/mod.rs",
    "clarity/src/vm/database/sqlite.rs",
    "clarity/src/vm/database/structures.rs",
    "clarity/src/vm/diagnostic.rs",
    "clarity/src/vm/errors.rs",
    "clarity/src/vm/events.rs",
    "clarity/src/vm/functions/arithmetic.rs",
    "clarity/src/vm/functions/assets.rs",
    "clarity/src/vm/functions/bitcoin.rs",
    "clarity/src/vm/functions/boolean.rs",
    "clarity/src/vm/functions/conversions.rs",
    "clarity/src/vm/functions/crypto.rs",
    "clarity/src/vm/functions/database.rs",
    "clarity/src/vm/functions/define.rs",
    "clarity/src/vm/functions/mod.rs",
    "clarity/src/vm/functions/options.rs",
    "clarity/src/vm/functions/post_conditions.rs",
    "clarity/src/vm/functions/principals.rs",
    "clarity/src/vm/functions/sequences.rs",
    "clarity/src/vm/functions/tuples.rs",
    "clarity/src/vm/hooks/internals.rs",
    "clarity/src/vm/hooks/mod.rs",
    "clarity/src/vm/hooks/trace.rs",
    "clarity/src/vm/mod.rs",
    "clarity/src/vm/representations.rs",
    "clarity/src/vm/resource_limiter.rs",
    "clarity/src/vm/tooling/mod.rs",
    "clarity/src/vm/types/mod.rs",
    "clarity/src/vm/types/serialization.rs",
    "clarity/src/vm/types/signatures.rs",
    "clarity/src/vm/variables.rs",
    "clarity/src/vm/version.rs",

    # -- stacks-codec: transaction and message wire encoding -------------------------------
    "stacks-codec/src/lib.rs",
    "stacks-codec/src/strings.rs",
    "stacks-codec/src/transaction.rs",

    # -- crates/stacks-transactions: standalone transaction and post-condition checks ------
    "crates/stacks-transactions/src/lib.rs",

    # -- stacks-common: addresses, hashing, secp256k1, codec and shared utils --------------
    "stacks-common/src/address/b58.rs",
    "stacks-common/src/address/c32.rs",
    "stacks-common/src/address/c32_old.rs",
    "stacks-common/src/address/mod.rs",
    "stacks-common/src/alloc_tracker.rs",
    "stacks-common/src/bitvec.rs",
    "stacks-common/src/codec/macros.rs",
    "stacks-common/src/codec/mod.rs",
    "stacks-common/src/libcommon.rs",
    "stacks-common/src/types/chainstate.rs",
    "stacks-common/src/types/mod.rs",
    "stacks-common/src/types/net.rs",
    "stacks-common/src/types/sqlite.rs",
    "stacks-common/src/util/chunked_encoding.rs",
    "stacks-common/src/util/db.rs",
    "stacks-common/src/util/ed25519.rs",
    "stacks-common/src/util/hash.rs",
    "stacks-common/src/util/log.rs",
    "stacks-common/src/util/lru_cache.rs",
    "stacks-common/src/util/macros.rs",
    "stacks-common/src/util/mod.rs",
    "stacks-common/src/util/pair.rs",
    "stacks-common/src/util/pipe.rs",
    "stacks-common/src/util/retry.rs",
    "stacks-common/src/util/secp256k1/mod.rs",
    "stacks-common/src/util/secp256k1/native.rs",
    "stacks-common/src/util/secp256k1/wasm.rs",
    "stacks-common/src/util/secp256r1.rs",
    "stacks-common/src/util/serde_serializers.rs",
    "stacks-common/src/util/uint.rs",
    "stacks-common/src/util/vrf.rs",

    # -- libsigner: signer transport, events and v0 messages -------------------------------
    "libsigner/src/error.rs",
    "libsigner/src/events.rs",
    "libsigner/src/http.rs",
    "libsigner/src/libsigner.rs",
    "libsigner/src/runloop.rs",
    "libsigner/src/session.rs",
    "libsigner/src/signer_set.rs",
    "libsigner/src/v0/messages.rs",
    "libsigner/src/v0/mod.rs",
    "libsigner/src/v0/signer_state.rs",

    # -- libstackerdb: StackerDB chunk signing and verification ----------------------------
    "libstackerdb/src/libstackerdb.rs",

    # -- pox-locking: the Rust side that locks and unlocks STX for PoX/stacking ------------
    "pox-locking/src/events.rs",
    "pox-locking/src/events_24.rs",
    "pox-locking/src/lib.rs",
    "pox-locking/src/pox_1.rs",
    "pox-locking/src/pox_2.rs",
    "pox-locking/src/pox_3.rs",
    "pox-locking/src/pox_4.rs",
    "pox-locking/src/pox_5.rs",

    # -- stacks-signer: the Nakamoto signer decision logic and chainstate view -------------
    "stacks-signer/src/chainstate/mod.rs",
    "stacks-signer/src/chainstate/v1.rs",
    "stacks-signer/src/chainstate/v2.rs",
    "stacks-signer/src/cli.rs",
    "stacks-signer/src/client/mod.rs",
    "stacks-signer/src/client/stackerdb.rs",
    "stacks-signer/src/client/stacks_client.rs",
    "stacks-signer/src/config.rs",
    "stacks-signer/src/lib.rs",
    "stacks-signer/src/main.rs",
    "stacks-signer/src/monitor_signers.rs",
    "stacks-signer/src/monitoring/mod.rs",
    "stacks-signer/src/monitoring/prometheus.rs",
    "stacks-signer/src/monitoring/server.rs",
    "stacks-signer/src/runloop.rs",
    "stacks-signer/src/signerdb.rs",
    "stacks-signer/src/utils.rs",
    "stacks-signer/src/v0/mod.rs",
    "stacks-signer/src/v0/signer.rs",
    "stacks-signer/src/v0/signer_state.rs",

    # -- stacks-node: the node binary, run loops, miner, burnchain and event dispatch ------
    "stacks-node/src/burnchains/bitcoin/core_controller.rs",
    "stacks-node/src/burnchains/bitcoin/mod.rs",
    "stacks-node/src/burnchains/bitcoin_regtest_controller.rs",
    "stacks-node/src/burnchains/mod.rs",
    "stacks-node/src/burnchains/rpc/bitcoin_rpc_client/mod.rs",
    "stacks-node/src/burnchains/rpc/mod.rs",
    "stacks-node/src/burnchains/rpc/rpc_transport/mod.rs",
    "stacks-node/src/event_dispatcher.rs",
    "stacks-node/src/event_dispatcher/db.rs",
    "stacks-node/src/event_dispatcher/payloads.rs",
    "stacks-node/src/event_dispatcher/stacker_db.rs",
    "stacks-node/src/event_dispatcher/worker.rs",
    "stacks-node/src/globals.rs",
    "stacks-node/src/keychain.rs",
    "stacks-node/src/main.rs",
    "stacks-node/src/monitoring/mod.rs",
    "stacks-node/src/monitoring/prometheus.rs",
    "stacks-node/src/nakamoto_node.rs",
    "stacks-node/src/nakamoto_node/miner.rs",
    "stacks-node/src/nakamoto_node/miner_db.rs",
    "stacks-node/src/nakamoto_node/peer.rs",
    "stacks-node/src/nakamoto_node/relayer.rs",
    "stacks-node/src/nakamoto_node/signer_coordinator.rs",
    "stacks-node/src/nakamoto_node/stackerdb_listener.rs",
    "stacks-node/src/neon_node.rs",
    "stacks-node/src/node.rs",
    "stacks-node/src/operations.rs",
    "stacks-node/src/run_loop/boot_nakamoto.rs",
    "stacks-node/src/run_loop/helium.rs",
    "stacks-node/src/run_loop/mod.rs",
    "stacks-node/src/run_loop/nakamoto.rs",
    "stacks-node/src/run_loop/neon.rs",
    "stacks-node/src/syncctl.rs",
    "stacks-node/src/tenure.rs",

    # -- stackslib: consensus, chainstate, the Clarity VM host, burn ops and the P2P/RPC network ----
    "stackslib/src/burnchains/bitcoin/address.rs",
    "stackslib/src/burnchains/bitcoin/bits.rs",
    "stackslib/src/burnchains/bitcoin/blocks.rs",
    "stackslib/src/burnchains/bitcoin/indexer.rs",
    "stackslib/src/burnchains/bitcoin/keys.rs",
    "stackslib/src/burnchains/bitcoin/messages.rs",
    "stackslib/src/burnchains/bitcoin/mod.rs",
    "stackslib/src/burnchains/bitcoin/network.rs",
    "stackslib/src/burnchains/bitcoin/spv.rs",
    "stackslib/src/burnchains/burnchain.rs",
    "stackslib/src/burnchains/db.rs",
    "stackslib/src/burnchains/indexer.rs",
    "stackslib/src/burnchains/mod.rs",
    "stackslib/src/chainstate/burn/atc.rs",
    "stackslib/src/chainstate/burn/db/mod.rs",
    "stackslib/src/chainstate/burn/db/processing.rs",
    "stackslib/src/chainstate/burn/db/sortdb.rs",
    "stackslib/src/chainstate/burn/distribution.rs",
    "stackslib/src/chainstate/burn/mod.rs",
    "stackslib/src/chainstate/burn/operations/delegate_stx.rs",
    "stackslib/src/chainstate/burn/operations/leader_block_commit.rs",
    "stackslib/src/chainstate/burn/operations/leader_key_register.rs",
    "stackslib/src/chainstate/burn/operations/mod.rs",
    "stackslib/src/chainstate/burn/operations/stack_stx.rs",
    "stackslib/src/chainstate/burn/operations/transfer_stx.rs",
    "stackslib/src/chainstate/burn/operations/vote_for_aggregate_key.rs",
    "stackslib/src/chainstate/burn/sortition.rs",
    "stackslib/src/chainstate/coordinator/comm.rs",
    "stackslib/src/chainstate/coordinator/mod.rs",
    "stackslib/src/chainstate/mod.rs",
    "stackslib/src/chainstate/nakamoto/coordinator/mod.rs",
    "stackslib/src/chainstate/nakamoto/keys.rs",
    "stackslib/src/chainstate/nakamoto/miner.rs",
    "stackslib/src/chainstate/nakamoto/mod.rs",
    "stackslib/src/chainstate/nakamoto/shadow.rs",
    "stackslib/src/chainstate/nakamoto/signer_set.rs",
    "stackslib/src/chainstate/nakamoto/staging_blocks.rs",
    "stackslib/src/chainstate/nakamoto/tenure.rs",
    "stackslib/src/chainstate/stacks/address.rs",
    "stackslib/src/chainstate/stacks/auth.rs",
    "stackslib/src/chainstate/stacks/block.rs",
    "stackslib/src/chainstate/stacks/boot/bns.clar",
    "stackslib/src/chainstate/stacks/boot/contract_tests.rs",
    "stackslib/src/chainstate/stacks/boot/cost-voting.clar",
    "stackslib/src/chainstate/stacks/boot/costs-2.clar",
    "stackslib/src/chainstate/stacks/boot/costs-3.clar",
    "stackslib/src/chainstate/stacks/boot/costs-4.clar",
    "stackslib/src/chainstate/stacks/boot/costs.clar",
    "stackslib/src/chainstate/stacks/boot/docs.rs",
    "stackslib/src/chainstate/stacks/boot/genesis.clar",
    "stackslib/src/chainstate/stacks/boot/lockup.clar",
    "stackslib/src/chainstate/stacks/boot/mod.rs",
    "stackslib/src/chainstate/stacks/boot/pox-2.clar",
    "stackslib/src/chainstate/stacks/boot/pox-3.clar",
    "stackslib/src/chainstate/stacks/boot/pox-4.clar",
    "stackslib/src/chainstate/stacks/boot/pox-5.clar",
    "stackslib/src/chainstate/stacks/boot/pox-mainnet.clar",
    "stackslib/src/chainstate/stacks/boot/pox.clar",
    "stackslib/src/chainstate/stacks/boot/pox_2_tests.rs",
    "stackslib/src/chainstate/stacks/boot/pox_3_tests.rs",
    "stackslib/src/chainstate/stacks/boot/pox_4_tests.rs",
    "stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar",
    "stackslib/src/chainstate/stacks/boot/signers-1-xxx.clar",
    "stackslib/src/chainstate/stacks/boot/signers-voting.clar",
    "stackslib/src/chainstate/stacks/boot/signers.clar",
    "stackslib/src/chainstate/stacks/boot/signers_tests.rs",
    "stackslib/src/chainstate/stacks/boot/sip-031.clar",
    "stackslib/src/chainstate/stacks/db/accounts.rs",
    "stackslib/src/chainstate/stacks/db/blocks.rs",
    "stackslib/src/chainstate/stacks/db/contracts.rs",
    "stackslib/src/chainstate/stacks/db/headers.rs",
    "stackslib/src/chainstate/stacks/db/mod.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/blocks.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/burnchain.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/clarity.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/common.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/fork_storage.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/index.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/mod.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/sortition.rs",
    "stackslib/src/chainstate/stacks/db/snapshot/spv.rs",
    "stackslib/src/chainstate/stacks/db/transactions.rs",
    "stackslib/src/chainstate/stacks/db/unconfirmed.rs",
    "stackslib/src/chainstate/stacks/events.rs",
    "stackslib/src/chainstate/stacks/index/bits.rs",
    "stackslib/src/chainstate/stacks/index/blob_layout.rs",
    "stackslib/src/chainstate/stacks/index/cache.rs",
    "stackslib/src/chainstate/stacks/index/file.rs",
    "stackslib/src/chainstate/stacks/index/marf.rs",
    "stackslib/src/chainstate/stacks/index/mod.rs",
    "stackslib/src/chainstate/stacks/index/node.rs",
    "stackslib/src/chainstate/stacks/index/profile.rs",
    "stackslib/src/chainstate/stacks/index/proofs.rs",
    "stackslib/src/chainstate/stacks/index/squash.rs",
    "stackslib/src/chainstate/stacks/index/squash/node_store.rs",
    "stackslib/src/chainstate/stacks/index/squash/stream.rs",
    "stackslib/src/chainstate/stacks/index/storage.rs",
    "stackslib/src/chainstate/stacks/index/trie.rs",
    "stackslib/src/chainstate/stacks/index/trie_sql.rs",
    "stackslib/src/chainstate/stacks/miner.rs",
    "stackslib/src/chainstate/stacks/mod.rs",
    "stackslib/src/chainstate/stacks/sbtc.rs",
    "stackslib/src/chainstate/stacks/transaction.rs",
    "stackslib/src/clarity_vm/clarity.rs",
    "stackslib/src/clarity_vm/database/ephemeral.rs",
    "stackslib/src/clarity_vm/database/marf.rs",
    "stackslib/src/clarity_vm/database/mod.rs",
    "stackslib/src/clarity_vm/mod.rs",
    "stackslib/src/clarity_vm/special.rs",
    "stackslib/src/config/chain_data.rs",
    "stackslib/src/config/mod.rs",
    "stackslib/src/core/mempool.rs",
    "stackslib/src/core/mod.rs",
    "stackslib/src/core/nonce_cache.rs",
    "stackslib/src/cost_estimates/fee_medians.rs",
    "stackslib/src/cost_estimates/fee_rate_fuzzer.rs",
    "stackslib/src/cost_estimates/fee_scalar.rs",
    "stackslib/src/cost_estimates/metrics.rs",
    "stackslib/src/cost_estimates/mod.rs",
    "stackslib/src/cost_estimates/pessimistic.rs",
    "stackslib/src/deps/mod.rs",
    "stackslib/src/lib.rs",
    "stackslib/src/monitoring/mod.rs",
    "stackslib/src/monitoring/prometheus.rs",
    "stackslib/src/net/api/blockreplay.rs",
    "stackslib/src/net/api/blocksimulate.rs",
    "stackslib/src/net/api/callreadonly.rs",
    "stackslib/src/net/api/fastcallreadonly.rs",
    "stackslib/src/net/api/get_tenure_tip_meta.rs",
    "stackslib/src/net/api/get_tenures_fork_info.rs",
    "stackslib/src/net/api/getaccount.rs",
    "stackslib/src/net/api/getattachment.rs",
    "stackslib/src/net/api/getattachmentsinv.rs",
    "stackslib/src/net/api/getblock.rs",
    "stackslib/src/net/api/getblock_v3.rs",
    "stackslib/src/net/api/getblockbyheight.rs",
    "stackslib/src/net/api/getclaritymarfvalue.rs",
    "stackslib/src/net/api/getclaritymetadata.rs",
    "stackslib/src/net/api/getconstantval.rs",
    "stackslib/src/net/api/getcontractabi.rs",
    "stackslib/src/net/api/getcontractsrc.rs",
    "stackslib/src/net/api/getdatavar.rs",
    "stackslib/src/net/api/getheaders.rs",
    "stackslib/src/net/api/gethealth.rs",
    "stackslib/src/net/api/getinfo.rs",
    "stackslib/src/net/api/getistraitimplemented.rs",
    "stackslib/src/net/api/getmapentry.rs",
    "stackslib/src/net/api/getmicroblocks_confirmed.rs",
    "stackslib/src/net/api/getmicroblocks_indexed.rs",
    "stackslib/src/net/api/getmicroblocks_unconfirmed.rs",
    "stackslib/src/net/api/getneighbors.rs",
    "stackslib/src/net/api/getpoxinfo.rs",
    "stackslib/src/net/api/getsigner.rs",
    "stackslib/src/net/api/getsortition.rs",
    "stackslib/src/net/api/getstackerdbchunk.rs",
    "stackslib/src/net/api/getstackerdbmetadata.rs",
    "stackslib/src/net/api/getstackers.rs",
    "stackslib/src/net/api/getstxtransfercost.rs",
    "stackslib/src/net/api/gettenure.rs",
    "stackslib/src/net/api/gettenureblocks.rs",
    "stackslib/src/net/api/gettenureblocksbyhash.rs",
    "stackslib/src/net/api/gettenureblocksbyheight.rs",
    "stackslib/src/net/api/gettenureinfo.rs",
    "stackslib/src/net/api/gettenuretip.rs",
    "stackslib/src/net/api/gettransaction.rs",
    "stackslib/src/net/api/gettransaction_unconfirmed.rs",
    "stackslib/src/net/api/liststackerdbreplicas.rs",
    "stackslib/src/net/api/mod.rs",
    "stackslib/src/net/api/postblock.rs",
    "stackslib/src/net/api/postblock_proposal.rs",
    "stackslib/src/net/api/postblock_v3.rs",
    "stackslib/src/net/api/postfeerate.rs",
    "stackslib/src/net/api/postmempoolquery.rs",
    "stackslib/src/net/api/postmicroblock.rs",
    "stackslib/src/net/api/poststackerdbchunk.rs",
    "stackslib/src/net/api/posttransaction.rs",
    "stackslib/src/net/api/read_only/mod.rs",
    "stackslib/src/net/api/read_only/parse.rs",
    "stackslib/src/net/api/txsimulate.rs",
    "stackslib/src/net/asn.rs",
    "stackslib/src/net/atlas/db.rs",
    "stackslib/src/net/atlas/download.rs",
    "stackslib/src/net/atlas/mod.rs",
    "stackslib/src/net/chat.rs",
    "stackslib/src/net/codec.rs",
    "stackslib/src/net/connection.rs",
    "stackslib/src/net/db.rs",
    "stackslib/src/net/dns.rs",
    "stackslib/src/net/download/epoch2x.rs",
    "stackslib/src/net/download/mod.rs",
    "stackslib/src/net/download/nakamoto/download_state_machine.rs",
    "stackslib/src/net/download/nakamoto/mod.rs",
    "stackslib/src/net/download/nakamoto/tenure.rs",
    "stackslib/src/net/download/nakamoto/tenure_downloader.rs",
    "stackslib/src/net/download/nakamoto/tenure_downloader_set.rs",
    "stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs",
    "stackslib/src/net/http/common.rs",
    "stackslib/src/net/http/error.rs",
    "stackslib/src/net/http/mod.rs",
    "stackslib/src/net/http/request.rs",
    "stackslib/src/net/http/response.rs",
    "stackslib/src/net/http/stream.rs",
    "stackslib/src/net/httpcore.rs",
    "stackslib/src/net/inv/epoch2x.rs",
    "stackslib/src/net/inv/mod.rs",
    "stackslib/src/net/inv/nakamoto.rs",
    "stackslib/src/net/mempool/mod.rs",
    "stackslib/src/net/mod.rs",
    "stackslib/src/net/neighbors/comms.rs",
    "stackslib/src/net/neighbors/db.rs",
    "stackslib/src/net/neighbors/mod.rs",
    "stackslib/src/net/neighbors/neighbor.rs",
    "stackslib/src/net/neighbors/rpc.rs",
    "stackslib/src/net/neighbors/walk.rs",
    "stackslib/src/net/p2p.rs",
    "stackslib/src/net/poll.rs",
    "stackslib/src/net/prune.rs",
    "stackslib/src/net/relay.rs",
    "stackslib/src/net/rpc.rs",
    "stackslib/src/net/server.rs",
    "stackslib/src/net/stackerdb/config.rs",
    "stackslib/src/net/stackerdb/db.rs",
    "stackslib/src/net/stackerdb/mod.rs",
    "stackslib/src/net/stackerdb/sync.rs",
    "stackslib/src/net/unsolicited.rs",
    "stackslib/src/util_lib/bloom.rs",
    "stackslib/src/util_lib/boot.rs",
    "stackslib/src/util_lib/db.rs",
    "stackslib/src/util_lib/mod.rs",
    "stackslib/src/util_lib/signed_structured_data.rs",
    "stackslib/src/util_lib/strings.rs",

    # =================================================================================
    # NOT AUDITED (excluded from every variant): tests, mocks and *test* files; fuzz and
    # bench harnesses; test_util and the hooks/testing render helpers; docs/ and README;
    # config, *.toml and CHANGELOG; generated tables (stx-genesis, genesis_data.rs) and
    # build.rs; vendored third-party code under deps_common/ (bitcoin, httparse, bech32,
    # ctrlc); the contrib/ tools and stacks-profiler; sample/ example contracts; and the
    # *-testnet / *.tests.clar network- and test-only contract bodies. A defect in any of
    # these is only in scope when it is reachable from the audited code above.
    # =================================================================================
]


target_scopes = [
    "Critical. AN AUTH-GATED ENDPOINT MUST FAIL CLOSED. `postblock_v3.rs` requires the `authorization` header to equal the configured password only when `broadcast=1`; `poststackerdbchunk.rs`, `postmempoolquery.rs`, `callreadonly.rs`/`fastcallreadonly.rs` and the simulate endpoints each gate on an optional configured secret and reject with 401 when it is absent or mismatched. Show a remote request that reaches a privileged action without the secret: a preamble whose `authorization` header comparison is case- or whitespace-normalised, a missing-config branch that treats `None` as 'open' instead of 'disabled', a `broadcast` flag parsed so the authenticated path runs unauthenticated, a header injected twice where the last wins. Identity: the set of requests that execute the gated action == the set carrying the exact configured secret, and no request executes it when no secret is configured.",

    "Critical. A STACKERDB CHUNK IS WRITABLE ONLY BY ITS SLOT OWNER. `libstackerdb.rs` (`StackerDBChunkData::verify`, `sign`), `stackerdb/db.rs` and `stackerdb/sync.rs` accept a chunk only when its signature recovers to the address that owns the slot and its version exceeds the stored version. Show a remote writer overwriting a slot they do not own or replaying an old chunk: a signature recovered over a hash that omits the slot id, version or contract so one signature validates another slot, a version comparison that accepts equal or lower versions, a sync path that stores a gossiped chunk before verifying its signature, a slot-to-owner mapping read from the wrong reward cycle. Identity: every stored or relayed StackerDB chunk == a chunk signed by the current owner of its slot, with a strictly greater version.",

    "Critical. THE NODE MUST NOT RELAY OR STORE WHAT A PEER NEVER AUTHENTICATED. `relay.rs`, `unsolicited.rs` and `chat.rs` decide which gossiped blocks, microblocks, transactions and StackerDB messages a node forwards and stores; a message accepted here propagates network-wide. Show an unsolicited or forged message the node relays without verifying its origin or contents: a `StacksMessage` whose payload is trusted before `verify` on the preamble, a block accepted from a peer that did not win its sortition and forwarded before validation, a relay-hint loop that amplifies one message, a transaction stored in the mempool from a relay path that skips `will_admit_mempool_tx`. Identity: every message a node relays or stores == a message whose origin and contents it has verified against consensus rules.",

    "Critical. THE P2P HANDSHAKE MUST BIND A PEER TO ITS CLAIMED IDENTITY AND NETWORK. `codec.rs`, `chat.rs` and `net/db.rs` verify the handshake signature, the `network_id`/`chain_id`, the peer public key and the sequence/nonce that gate a session. Show a remote peer impersonating another, replaying a handshake, or crossing networks: a handshake signature verified over a message that omits the peer address or network id, a nonce or sequence accepted out of order so a replayed authenticated frame is processed, a `Preamble` whose length fields let a later message body be reinterpreted, a peer inserted into the frontier DB under an identity it did not prove. Identity: the peer identity and network the node associates with a connection == the identity and network the handshake signature actually authenticated.",

    "Critical. EVERY LENGTH-PREFIXED FIELD FROM THE WIRE MUST BE BOUNDS-CHECKED. `codec.rs`, `net/http/request.rs`, `net/http/stream.rs`, `httpcore.rs` and the `consensus_deserialize` implementations read counts and lengths an attacker chooses and allocate or index on them, bounded by `MAX_MESSAGE_LEN` / `MAX_PAYLOAD_LEN` and per-field caps. Show a remote message that causes an out-of-bounds read, an unchecked allocation sized by a wire field, an integer overflow in a length computation, a chunked-encoding or content-length mismatch that desynchronises the stream so the next request is attacker-framed, or a panic (`unwrap`, slice index, `expect`) reachable from parsing. Name the impact: remote crash (unauthenticated DoS of the node), memory disclosure, or request smuggling. Identity: bytes a handler reads for a field == bytes the validated length said were present, for every field an attacker sizes.",

    "Critical. A READ ENDPOINT MUST NOT RUN UNBOUNDED CLARITY OR SERVE ANOTHER FORK'S STATE. `callreadonly.rs` / `fastcallreadonly.rs` execute caller-supplied Clarity against a caller-named tip with a cost limit; `getmapentry.rs`, `getdatavar.rs`, `getclaritymarfvalue.rs`, `getstackerdbchunk.rs` and `postfeerate.rs` read state at a caller-named block. Show a remote caller running Clarity past the intended cost/read bound (a `fastcallreadonly` limiter that resets between sub-calls, a read-only call that mutates through a trait), reading state from a block on a non-canonical fork or an unconfirmed tip as if canonical, or a fee-rate estimate an attacker steers by crafted input. Name the impact: unauthenticated compute DoS, or a wallet/bridge served state that no canonical block committed. Identity: the state and cost a read endpoint returns == the state committed at the requested canonical block, within the configured bound.",

    "High. ATTACHMENT AND ATTACHMENT-INVENTORY GOSSIP MUST MATCH THEIR COMMITTED HASH. `atlas/mod.rs`, `atlas/db.rs`, `atlas/download.rs`, `getattachment.rs` and `getattachmentsinv.rs` store and serve BNS attachments keyed by content hash, gossiped from peers. Show a peer serving an attachment whose bytes do not match the requested hash, poisoning the inventory so a valid attachment is deemed absent, or filling storage with attachments no on-chain name commits to. Name the impact: BNS resolution serving wrong data, or attachment storage exhaustion tied to a consensus commitment. Identity: the attachment bytes served for a hash == the bytes whose hash a confirmed name operation committed.",

    "High. THE INVENTORY AND TENURE DOWNLOAD STATE MACHINE MUST NOT BE STEERED BY A PEER. `inv/nakamoto.rs`, `download/nakamoto/*` decide which tenures and blocks to fetch from which peer based on advertised inventories. Show a peer advertising a false inventory that makes the node skip a canonical tenure, loop re-downloading, accept a block for the wrong tenure slot, or wedge the download state machine so the node cannot follow the chain tip. Name the impact: the node stalls behind the canonical tip (availability) or accepts a mis-slotted block into staging. Identity: the tenure/block the node fetches and stages for a slot == the tenure/block the canonical inventory (verified against sortition) names for that slot.",

    "High. THE SIGNER EVENT STREAM MUST DELIVER ONLY WHAT THE SENDER SIGNED. `libsigner/src/http.rs`, `session.rs`, `events.rs` and `v0/messages.rs` frame and parse the StackerDB/event messages the signer binary consumes. Show a remote sender injecting a message the signer treats as authentic - a `SignerMessage` parsed before its origin is checked, a length field that lets one event body be read as another, a stale message replayed into the stream - so the signer acts on data no authorized party sent. Name the impact bounded to the signer transport (the consensus decision itself is another variant). Identity: every message the signer library surfaces to the runloop == a message an authorized StackerDB slot owner signed.",

    "Critical. THE MISSING INVARIANT - what nobody built. No single choke point guarantees every remote byte is authenticated before it influences state: auth-gated endpoints each re-implement the secret check and can fail open; relay and unsolicited paths trust some messages before verification; length fields from the wire are bounds-checked field-by-field with no global guarantee; read endpoints trust a caller-named tip; StackerDB sync and gossip verify signatures at different points. Identify the FIRST remotely reachable point where an unauthenticated or unauthorized message influences stored state, relayed gossip, served state, or crashes the node, prove it with a Rust test in `stackslib::net` (or `libsigner`) that feeds crafted bytes to the handler and asserts either the authenticated-versus-stored equality or a panic/over-read, and show the impact is remote (an open port), needs no privileged role, and is one of: node crash / unauthenticated DoS, network-wide propagation of forged data, or state served that no canonical block committed.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate network-boundary (P2P/RPC/StackerDB/Atlas) audit questions for one
    stacks-core target.

    ```
    target_file format:
    "'File Name: stackslib/src/net/relay.rs -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate blockchain-node network-security audit questions for this exact stacks-core
    target:

    {target_file}

    Project focus:
    stacks-core exposes P2P and RPC ports that accept bytes from anyone. Untrusted input
    arrives as P2P handshakes and gossiped blocks/txs/StackerDB messages, HTTP requests to
    the RPC API, StackerDB chunks, Atlas attachments, and advertised inventories that steer
    the download state machine. The node decides (a) whether a peer is who it claims and
    allowed to say this - handshake signatures, StackerDB slot-owner signatures, auth-gated
    endpoint secrets; (b) whether stored or relayed state matches what was actually
    authorized; (c) whether each handler stays within its resource and trust bounds. Anything
    the node stores, relays or serves that a remote party did not authenticate, plus any
    remotely reachable panic or over-read, is the bug.

    Rules:
    * Treat `File Name:` as the exact file.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Rust symbols (function, struct, enum variant, constant like MAX_MESSAGE_LEN,
      trait) as they appear in the file.
    * EVERY question must close on an equality that must hold - authenticated-versus-stored,
      served-versus-committed, bytes-read-versus-length - OR name a specific remotely
      reachable memory/panic fault. State it explicitly. Vague questions are rejected.
    * Attacker is unprivileged only: any remote party who can open a TCP connection to a
      node's P2P or RPC port and send arbitrary bytes, run their own peer, own a StackerDB
      slot they legitimately hold, and gossip messages. They do NOT hold the node's
      configured RPC secret, another peer's or slot owner's key, or any admin role.
    * Attacker is NOT the node operator, not a configured trusted peer with the secret, not a
      signer or miner with another's key. No compromised dependency; no social engineering; no
      physical or local-network access to the victim node.
    * PROGRAM EXCLUSIONS - a question landing in any of these wastes the whole batch:
      - epoch2x/neon pre-Nakamoto download and inv paths, the signer decision logic
        (stacks-signer runloop/signerdb), and consensus block-validation internals are other
        variants and OUT OF SCOPE here, as are README, tests, benches and config.
      - Generic volumetric DDoS, bandwidth flooding and connection-slot exhaustion that only
        require traffic volume are OUT OF SCOPE; a single-message crash, over-read, request
        smuggling or amplification IS in scope (name it).
      - Defects in tokio, rustls, serde or the OS TCP stack with no exploit path through this
        repo's code are OUT OF SCOPE; a weakness here that misuses them is IN scope.
      - Also excluded: leaked keys, privileged accounts, centralization risk, best-practice
        notes, feature requests, missing HTTP security headers with no impact, and
        theoretical findings.
    * IN-SCOPE IMPACTS - every question must land on one and name it:
      Critical: remote node crash or unauthenticated DoS from a single or few messages;
      unauthenticated/unauthorized write to node state or StackerDB; network-wide propagation
      of forged blocks/txs/chunks; request smuggling or auth bypass on a gated endpoint;
      memory disclosure.
      High: serving state from a non-canonical block as canonical; steering a node off the
      canonical tip via false inventory; attachment/BNS data mismatch; a bounded compute DoS
      on a read endpoint.
    * Every question must be a concrete real-world scenario a remote unprivileged party can
      execute against a node's open port.
    * A rejection is a finding only when it drops a valid message permanently or accepts a
      forged one - say which.
    * Generate 20 to 40 high-signal questions.
    * At least 70% must land on a Critical impact rather than a High one.
    * Every question must be testable with a Rust test in `stackslib::net` or `libsigner`
      feeding crafted bytes to the handler locally. Never propose testing on mainnet or a
      public testnet.
    * Avoid generic checklist questions and repeated root causes.
    * Prefer questions that name TWO values that must be equal (authenticated vs stored,
      served vs committed, bytes-read vs length) or a precise panic/over-read site.

    Known dead ends - do NOT generate questions about these:
    * Anything needing the node's RPC secret, another peer's or slot owner's key, or an admin role.
    * Volumetric DDoS, bandwidth or connection-slot flooding needing only traffic volume.
    * A dependency CVE with no reachable path through this repo's net code.
    * Findings only in epoch2x/neon paths, the signer decision logic, or tests/tooling.

    Core equalities / faults (each question must close on one):
    * AUTHENTICATION: what the node stores/relays/acts on == what a remote party's signature
      or configured secret authenticated.
    * OWNERSHIP: every StackerDB chunk stored/relayed == one signed by its slot's current
      owner, with a greater version.
    * CANONICITY: state a read endpoint serves == state committed at the requested canonical block.
    * BOUNDS: bytes a handler reads for a field == the validated length; no allocation or
      index on an unchecked wire value.
    * SAFETY: a named remotely reachable panic, over-read, smuggling or amplification site.

    Each question must include:
    1. target function, struct or endpoint;
    2. attacker action (a concrete message or request with the fields that matter);
    3. preconditions (peer state, config, reward cycle, tip);
    4. call sequence through framing, verification and storage/relay;
    5. the equality or fault, written explicitly;
    6. scoped impact and what is crashed, forged or exposed;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Method: function_or_endpoint] Can a remote unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, breaking the equality/fault EQUALITY, causing scoped impact: SCOPE_IMPACT against PARTY? Proof idea: Rust net test PARAMETERS asserting AUTHENTICATION, OWNERSHIP, CANONICITY, BOUNDS, or SAFETY.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a network-boundary exploit-validation prompt for stacks-core.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: any remote party who can connect to a node's P2P or RPC port and send arbitrary bytes, run their own peer, own a StackerDB slot they legitimately hold, and gossip messages. They do not hold the node's RPC secret, another peer's or slot owner's key, or any admin role, and have no local or physical access.
- Reject compromised-dependency, social-engineering and local/physical-access assumptions, and any path requiring a privileged role or the configured secret.
- OUT OF SCOPE, reject on sight: epoch2x/neon download and inv paths, the signer decision logic, consensus block-validation internals; README, tests, benches, config; volumetric DDoS, bandwidth flooding and connection-slot exhaustion needing only traffic volume; tokio/rustls/serde/OS-TCP defects with no exploit path through this repo's code; missing HTTP headers with no impact; best-practice notes; theoretical findings.
- The impact must be one of: Critical - remote crash/unauthenticated DoS from few messages, unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data, request smuggling or auth bypass, memory disclosure; High - serving non-canonical state as canonical, steering a node off the tip via false inventory, attachment/BNS mismatch, bounded compute DoS on a read endpoint.
- Focus on real impact: a forged message stored/relayed, a crash from a single message, or state served that no canonical block committed.

## Validate
- Write the equality or fault the question claims BEFORE tracing any code.
- Trace the exact reachable path from the remote bytes and record every verification (signature, secret, length, version) and every store/relay/serve, and every allocation or index on a wire-controlled value.
- Evaluate the equality before and after, or locate the exact panic/over-read site. If the guard holds, output no vulnerability.
- Check whether the handshake/chunk signature check, the auth-gate, `MAX_MESSAGE_LEN`/`MAX_PAYLOAD_LEN` and per-field caps, `will_admit_mempool_tx`, or the canonical-tip resolution already prevents it.
- State what the attacker achieves per message and whether it is repeatable, and confirm the port is remotely reachable with no privileged role.
- Require exact file/function support and a reproducible Rust test feeding crafted bytes to the handler.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[The broken equality or fault, the code path, root cause, the attacker's exact message, exploit flow, and why existing guards fail]

### Impact Explanation
[What is crashed, forged, written, smuggled or exposed, which party/nodes, repeatability, matching severity category]

### Likelihood Explanation
[Preconditions, peer/config/tip state required, attacker cost, remote reachability, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Rust net test plan feeding crafted bytes, with the exact assertion or crash site]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for stacks-core network claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- A claim is only valid if the report states the broken equality (authenticated vs stored, served vs committed, bytes vs length) or names a precise remotely reachable panic/over-read, and shows it concretely. Reject prose-only claims.
- Reject anything requiring the node's RPC secret, another peer's or slot owner's key, an admin role, local or physical access, a compromised dependency, or social engineering.
- OUT OF SCOPE, reject on sight: epoch2x/neon download and inv paths, the signer decision logic, consensus block-validation internals; README, tests, benches, config; volumetric DDoS, bandwidth flooding and connection-slot exhaustion needing only traffic volume; tokio/rustls/serde/OS-TCP defects with no exploit path through this repo's code; missing HTTP headers with no impact; centralization risk; best-practice notes; feature requests; theoretical findings.
- The impact must be one of: Critical - remote crash/unauthenticated DoS from few messages, unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data, request smuggling or auth bypass, memory disclosure; High - serving non-canonical state as canonical, steering a node off the tip via false inventory, attachment/BNS mismatch, bounded compute DoS on a read endpoint.
- Reject claims that need only traffic volume, or whose only effect is on the attacker's own node.
- Reject if the bug was already fixed, publicly disclosed, or covered by a known-issues list.
- A valid report must be triggerable by a remote unprivileged party against a node's open port on the current code.
- A PoC is mandatory. Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function/struct/endpoint, and line references.
2. The equality or fault written explicitly, with both sides or the crash site shown.
3. Clear root cause: which auth gap, ownership check, canonicity assumption, bounds check, or unsafe parse causes it.
4. Reachable exploit path: preconditions -> remote bytes -> framing, verification and storage/relay sequence -> observed divergence or fault.
5. The handshake/chunk signature check, the auth-gate, the length caps, `will_admit_mempool_tx`, and canonical-tip resolution reviewed and shown insufficient.
6. Impact stated concretely: what is crashed, forged, written or exposed, and whether it is remote and repeatable.
7. Reproducible proof: Rust test feeding crafted bytes to the handler with the asserted values or crash.

## Silent Triage Questions
Before output, internally answer:
- What exactly is the equality or fault, and does it actually occur?
- Can a remote party trigger it over an open port with no secret and no other party's key?
- Is the flaw in this repo's net/libsigner code, not in a dependency or the OS stack?
- What is crashed, forged, written or exposed, and can it be repeated remotely?
- Would an Immunefi triager accept it under the remotely-exploitable / DoS severity system?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the broken equality/fault and impact]

## Finding Description
[Exact code path, the equality or fault, root cause, exploit flow, and why existing guards fail]

## Impact Explanation
[What is crashed, forged, written, smuggled or exposed, affected party/nodes, repeatability, severity category]

## Likelihood Explanation
[Attacker capability, preconditions, remote reachability, cost, feasibility]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or Rust net test plan with concrete assertions or crash site]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for the stacks-core network boundary.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope repo context only (`stackslib/src/net/**` excluding epoch2x/neon paths, `libstackerdb/**`, and the `libsigner` transport files). Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only remote, unprivileged analogs that break an equality (authenticated vs stored, served vs committed, bytes vs length) or reach a precise panic/over-read: an auth-gate that fails open, a StackerDB chunk stored without a valid owner signature, forged gossip relayed, an unchecked wire length, or non-canonical state served as canonical.
- OUT OF SCOPE, reject on sight: epoch2x/neon paths, the signer decision logic, consensus block-validation internals; README, tests, benches, config; volumetric DDoS, bandwidth flooding and connection-slot exhaustion needing only traffic volume; tokio/rustls/serde/OS-TCP defects with no path through this repo; anything requiring the node secret, another party's key or an admin role; missing HTTP headers with no impact; best-practice notes; theoretical findings.
- The impact must be one of: Critical - remote crash/unauthenticated DoS from few messages, unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data, request smuggling or auth bypass, memory disclosure; High - serving non-canonical state as canonical, steering a node off the tip via false inventory, attachment/BNS mismatch, bounded compute DoS on a read endpoint.
- Reject analogs needing only traffic volume or affecting only the attacker's own node.

## Validate
- Map the bug class to the strongest reachable path in this repo and state the equality or fault it would break.
- Evaluate both sides before and after, or locate the exact fault site.
- Prove root cause with exact file/function support.
- Accept only concrete remote crash, unauthorized write, forged-data propagation, auth bypass, smuggling, memory disclosure, or non-canonical/mismatched data served.

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
