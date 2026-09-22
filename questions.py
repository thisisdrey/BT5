import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 12
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'golang/go'
# todo: the name of the repository
REPO_NAME = 'go'

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
    # Module provenance, download, sumdb, cache and build
    "src/cmd/go/internal/auth/auth.go",
    "src/cmd/go/internal/auth/gitauth.go",
    "src/cmd/go/internal/auth/httputils.go",
    "src/cmd/go/internal/auth/netrc.go",
    "src/cmd/go/internal/auth/userauth.go",
    "src/cmd/go/internal/cache/cache.go",
    "src/cmd/go/internal/cache/default.go",
    "src/cmd/go/internal/cache/hash.go",
    "src/cmd/go/internal/cache/prog.go",
    "src/cmd/go/internal/cacheprog/cacheprog.go",
    "src/cmd/go/internal/fsys/fsys.go",
    "src/cmd/go/internal/fsys/glob.go",
    "src/cmd/go/internal/fsys/walk.go",
    "src/cmd/go/internal/imports/build.go",
    "src/cmd/go/internal/imports/read.go",
    "src/cmd/go/internal/imports/scan.go",
    "src/cmd/go/internal/imports/tags.go",
    "src/cmd/go/internal/load/flag.go",
    "src/cmd/go/internal/load/godebug.go",
    "src/cmd/go/internal/load/path.go",
    "src/cmd/go/internal/load/pkg.go",
    "src/cmd/go/internal/load/printer.go",
    "src/cmd/go/internal/load/search.go",
    "src/cmd/go/internal/modfetch/bootstrap.go",
    "src/cmd/go/internal/modfetch/cache.go",
    "src/cmd/go/internal/modfetch/codehost/codehost.go",
    "src/cmd/go/internal/modfetch/codehost/git.go",
    "src/cmd/go/internal/modfetch/codehost/svn.go",
    "src/cmd/go/internal/modfetch/codehost/vcs.go",
    "src/cmd/go/internal/modfetch/coderepo.go",
    "src/cmd/go/internal/modfetch/fetch.go",
    "src/cmd/go/internal/modfetch/key.go",
    "src/cmd/go/internal/modfetch/proxy.go",
    "src/cmd/go/internal/modfetch/repo.go",
    "src/cmd/go/internal/modfetch/sumdb.go",
    "src/cmd/go/internal/modfetch/toolchain.go",
    "src/cmd/go/internal/modload/build.go",
    "src/cmd/go/internal/modload/buildlist.go",
    "src/cmd/go/internal/modload/edit.go",
    "src/cmd/go/internal/modload/help.go",
    "src/cmd/go/internal/modload/import.go",
    "src/cmd/go/internal/modload/init.go",
    "src/cmd/go/internal/modload/list.go",
    "src/cmd/go/internal/modload/load.go",
    "src/cmd/go/internal/modload/modfile.go",
    "src/cmd/go/internal/modload/mvs.go",
    "src/cmd/go/internal/modload/query.go",
    "src/cmd/go/internal/modload/search.go",
    "src/cmd/go/internal/modload/vendor.go",
    "src/cmd/go/internal/modindex/build.go",
    "src/cmd/go/internal/modindex/build_read.go",
    "src/cmd/go/internal/modindex/read.go",
    "src/cmd/go/internal/modindex/scan.go",
    "src/cmd/go/internal/modindex/write.go",
    "src/cmd/go/internal/mvs/errors.go",
    "src/cmd/go/internal/mvs/graph.go",
    "src/cmd/go/internal/mvs/mvs.go",
    "src/cmd/go/internal/vcs/discovery.go",
    "src/cmd/go/internal/vcs/vcs.go",
    "src/cmd/go/internal/web/api.go",
    "src/cmd/go/internal/web/bootstrap.go",
    "src/cmd/go/internal/web/http.go",
    "src/cmd/go/internal/web/intercept/intercept.go",
    "src/cmd/go/internal/web/url.go",
    "src/cmd/go/internal/web/url_other.go",
    "src/cmd/go/internal/web/url_windows.go",
    "src/cmd/go/internal/work/action.go",
    "src/cmd/go/internal/work/build.go",
    "src/cmd/go/internal/work/buildid.go",
    "src/cmd/go/internal/work/cover.go",
    "src/cmd/go/internal/work/exec.go",
    "src/cmd/go/internal/work/gc.go",
    "src/cmd/go/internal/work/gccgo.go",
    "src/cmd/go/internal/work/init.go",
    "src/cmd/go/internal/work/security.go",
    "src/cmd/go/internal/work/shell.go",
    "src/cmd/go/internal/toolchain/exec.go",
    "src/cmd/go/internal/toolchain/exec_stub.go",
    "src/cmd/go/internal/toolchain/path_none.go",
    "src/cmd/go/internal/toolchain/path_plan9.go",
    "src/cmd/go/internal/toolchain/path_unix.go",
    "src/cmd/go/internal/toolchain/path_windows.go",
    "src/cmd/go/internal/toolchain/select.go",
    "src/cmd/go/internal/toolchain/switch.go",
    "src/cmd/go/internal/toolchain/umask_none.go",
    "src/cmd/go/internal/toolchain/umask_unix.go",
    "src/cmd/go/internal/gover/gomod.go",
    "src/cmd/go/internal/gover/gover.go",
    "src/cmd/go/internal/gover/local.go",
    "src/cmd/go/internal/gover/mod.go",
    "src/cmd/go/internal/gover/toolchain.go",
    "src/cmd/go/internal/gover/version.go",
    "src/cmd/go/internal/str/path.go",
    "src/cmd/go/internal/str/str.go",

    # HTTP request parsing and routing
    "src/net/http/cgi/cgi_main.go",
    "src/net/http/cgi/child.go",
    "src/net/http/cgi/host.go",
    "src/net/http/client.go",
    "src/net/http/clientconn.go",
    "src/net/http/clone.go",
    "src/net/http/cookie.go",
    "src/net/http/cookiejar/jar.go",
    "src/net/http/cookiejar/punycode.go",
    "src/net/http/csrf.go",
    "src/net/http/doc.go",
    "src/net/http/fcgi/child.go",
    "src/net/http/fcgi/fcgi.go",
    "src/net/http/filetransport.go",
    "src/net/http/fs.go",
    "src/net/http/header.go",
    "src/net/http/http.go",
    "src/net/http/http2.go",
    "src/net/http/http3.go",
    "src/net/http/httptrace/trace.go",
    "src/net/http/httputil/dump.go",
    "src/net/http/httputil/httputil.go",
    "src/net/http/httputil/persist.go",
    "src/net/http/httputil/reverseproxy.go",
    "src/net/http/internal/ascii/print.go",
    "src/net/http/internal/chunked.go",
    "src/net/http/internal/common.go",
    "src/net/http/internal/http2/api.go",
    "src/net/http/internal/http2/ciphers.go",
    "src/net/http/internal/http2/client_conn_pool.go",
    "src/net/http/internal/http2/config.go",
    "src/net/http/internal/http2/databuffer.go",
    "src/net/http/internal/http2/errors.go",
    "src/net/http/internal/http2/flow.go",
    "src/net/http/internal/http2/frame.go",
    "src/net/http/internal/http2/gotrack.go",
    "src/net/http/internal/http2/http2.go",
    "src/net/http/internal/http2/pipe.go",
    "src/net/http/internal/http2/server.go",
    "src/net/http/internal/http2/transport.go",
    "src/net/http/internal/http2/write.go",
    "src/net/http/internal/http2/writesched.go",
    "src/net/http/internal/http2/writesched_priority_rfc9218.go",
    "src/net/http/internal/http2/writesched_roundrobin.go",
    "src/net/http/internal/http3/body.go",
    "src/net/http/internal/http3/conn.go",
    "src/net/http/internal/http3/doc.go",
    "src/net/http/internal/http3/errors.go",
    "src/net/http/internal/http3/http3.go",
    "src/net/http/internal/http3/qpack.go",
    "src/net/http/internal/http3/qpack_decode.go",
    "src/net/http/internal/http3/qpack_encode.go",
    "src/net/http/internal/http3/qpack_static.go",
    "src/net/http/internal/http3/quic.go",
    "src/net/http/internal/http3/roundtrip.go",
    "src/net/http/internal/http3/server.go",
    "src/net/http/internal/http3/settings.go",
    "src/net/http/internal/http3/stream.go",
    "src/net/http/internal/http3/transport.go",
    "src/net/http/internal/http3/varint.go",
    "src/net/http/internal/httpcommon/ascii.go",
    "src/net/http/internal/httpcommon/gzip.go",
    "src/net/http/internal/httpcommon/headermap.go",
    "src/net/http/internal/httpcommon/request.go",
    "src/net/http/internal/sniff.go",
    "src/net/http/jar.go",
    "src/net/http/mapping.go",
    "src/net/http/method.go",
    "src/net/http/omithttp2.go",
    "src/net/http/pattern.go",
    "src/net/http/pprof/pprof.go",
    "src/net/http/request.go",
    "src/net/http/response.go",
    "src/net/http/responsecontroller.go",
    "src/net/http/roundtrip.go",
    "src/net/http/roundtrip_js.go",
    "src/net/http/routing_index.go",
    "src/net/http/routing_tree.go",
    "src/net/http/servemux121.go",
    "src/net/http/server.go",
    "src/net/http/sniff.go",
    "src/net/http/status.go",
    "src/net/http/transfer.go",
    "src/net/http/transport.go",
    "src/net/http/transport_default_other.go",
    "src/net/http/transport_default_wasm.go",
    "src/net/textproto/header.go",
    "src/net/textproto/pipeline.go",
    "src/net/textproto/reader.go",
    "src/net/textproto/textproto.go",
    "src/net/textproto/writer.go",
    "src/net/url/url.go",

    # TLS, X.509 and cryptographic verification
    "src/crypto/tls/alert.go",
    "src/crypto/tls/auth.go",
    "src/crypto/tls/cache.go",
    "src/crypto/tls/cipher_suites.go",
    "src/crypto/tls/common.go",
    "src/crypto/tls/conn.go",
    "src/crypto/tls/defaults.go",
    "src/crypto/tls/defaults_boring.go",
    "src/crypto/tls/defaults_fips140.go",
    "src/crypto/tls/ech.go",
    "src/crypto/tls/fipsonly/fipsonly.go",
    "src/crypto/tls/handshake_client.go",
    "src/crypto/tls/handshake_client_tls13.go",
    "src/crypto/tls/handshake_messages.go",
    "src/crypto/tls/handshake_server.go",
    "src/crypto/tls/handshake_server_tls13.go",
    "src/crypto/tls/internal/fips140tls/fipstls.go",
    "src/crypto/tls/key_agreement.go",
    "src/crypto/tls/key_schedule.go",
    "src/crypto/tls/prf.go",
    "src/crypto/tls/quic.go",
    "src/crypto/tls/ticket.go",
    "src/crypto/tls/tls.go",
    "src/crypto/x509/cert_pool.go",
    "src/crypto/x509/constraints.go",
    "src/crypto/x509/internal/macos/corefoundation.go",
    "src/crypto/x509/internal/macos/security.go",
    "src/crypto/x509/oid.go",
    "src/crypto/x509/parser.go",
    "src/crypto/x509/pem_decrypt.go",
    "src/crypto/x509/pkcs1.go",
    "src/crypto/x509/pkcs8.go",
    "src/crypto/x509/pkix/pkix.go",
    "src/crypto/x509/root.go",
    "src/crypto/x509/root_aix.go",
    "src/crypto/x509/root_bsd.go",
    "src/crypto/x509/root_darwin.go",
    "src/crypto/x509/root_linux.go",
    "src/crypto/x509/root_plan9.go",
    "src/crypto/x509/root_solaris.go",
    "src/crypto/x509/root_unix.go",
    "src/crypto/x509/root_wasm.go",
    "src/crypto/x509/root_windows.go",
    "src/crypto/x509/sec1.go",
    "src/crypto/x509/verify.go",
    "src/crypto/x509/x509.go",
    "src/crypto/rsa/boring.go",
    "src/crypto/rsa/fips.go",
    "src/crypto/rsa/notboring.go",
    "src/crypto/rsa/pkcs1v15.go",
    "src/crypto/rsa/rsa.go",
    "src/crypto/ecdsa/boring.go",
    "src/crypto/ecdsa/ecdsa.go",
    "src/crypto/ecdsa/ecdsa_legacy.go",
    "src/crypto/ecdsa/notboring.go",
    "src/crypto/ed25519/ed25519.go",
    "src/crypto/ecdh/ecdh.go",
    "src/crypto/ecdh/nist.go",
    "src/crypto/ecdh/x25519.go",
    "src/crypto/elliptic/elliptic.go",
    "src/crypto/elliptic/nistec.go",
    "src/crypto/elliptic/params.go",
    "src/crypto/internal/fips140/aes/aes.go",
    "src/crypto/internal/fips140/aes/aes_asm.go",
    "src/crypto/internal/fips140/aes/aes_generic.go",
    "src/crypto/internal/fips140/aes/aes_noasm.go",
    "src/crypto/internal/fips140/aes/aes_s390x.go",
    "src/crypto/internal/fips140/aes/cast.go",
    "src/crypto/internal/fips140/aes/cbc.go",
    "src/crypto/internal/fips140/aes/cbc_noasm.go",
    "src/crypto/internal/fips140/aes/cbc_ppc64x.go",
    "src/crypto/internal/fips140/aes/cbc_s390x.go",
    "src/crypto/internal/fips140/aes/const.go",
    "src/crypto/internal/fips140/aes/ctr.go",
    "src/crypto/internal/fips140/aes/ctr_asm.go",
    "src/crypto/internal/fips140/aes/ctr_noasm.go",
    "src/crypto/internal/fips140/aes/ctr_s390x.go",
    "src/crypto/internal/fips140/aes/gcm/cast.go",
    "src/crypto/internal/fips140/aes/gcm/cmac.go",
    "src/crypto/internal/fips140/aes/gcm/ctrkdf.go",
    "src/crypto/internal/fips140/aes/gcm/gcm.go",
    "src/crypto/internal/fips140/aes/gcm/gcm_asm.go",
    "src/crypto/internal/fips140/aes/gcm/gcm_generic.go",
    "src/crypto/internal/fips140/aes/gcm/gcm_noasm.go",
    "src/crypto/internal/fips140/aes/gcm/gcm_nonces.go",
    "src/crypto/internal/fips140/aes/gcm/gcm_ppc64x.go",
    "src/crypto/internal/fips140/aes/gcm/gcm_s390x.go",
    "src/crypto/internal/fips140/aes/gcm/ghash.go",
    "src/crypto/internal/fips140/alias/alias.go",
    "src/crypto/internal/fips140/asan.go",
    "src/crypto/internal/fips140/bigmod/nat.go",
    "src/crypto/internal/fips140/bigmod/nat_asm.go",
    "src/crypto/internal/fips140/bigmod/nat_noasm.go",
    "src/crypto/internal/fips140/bigmod/nat_wasm.go",
    "src/crypto/internal/fips140/boring.go",
    "src/crypto/internal/fips140/cast.go",
    "src/crypto/internal/fips140/check/check.go",
    "src/crypto/internal/fips140/drbg/cast.go",
    "src/crypto/internal/fips140/drbg/ctrdrbg.go",
    "src/crypto/internal/fips140/drbg/entropy_fips140.go",
    "src/crypto/internal/fips140/drbg/entropy_wasm.go",
    "src/crypto/internal/fips140/drbg/rand.go",
    "src/crypto/internal/fips140/ecdh/cast.go",
    "src/crypto/internal/fips140/ecdh/ecdh.go",
    "src/crypto/internal/fips140/ecdsa/cast.go",
    "src/crypto/internal/fips140/ecdsa/ecdsa.go",
    "src/crypto/internal/fips140/ecdsa/ecdsa_noasm.go",
    "src/crypto/internal/fips140/ecdsa/ecdsa_s390x.go",
    "src/crypto/internal/fips140/ecdsa/hmacdrbg.go",
    "src/crypto/internal/fips140/ed25519/cast.go",
    "src/crypto/internal/fips140/ed25519/ed25519.go",
    "src/crypto/internal/fips140/edwards25519/doc.go",
    "src/crypto/internal/fips140/edwards25519/edwards25519.go",
    "src/crypto/internal/fips140/edwards25519/field/fe.go",
    "src/crypto/internal/fips140/edwards25519/field/fe_amd64_noasm.go",
    "src/crypto/internal/fips140/edwards25519/field/fe_generic.go",
    "src/crypto/internal/fips140/edwards25519/scalar.go",
    "src/crypto/internal/fips140/edwards25519/scalarmult.go",
    "src/crypto/internal/fips140/edwards25519/tables.go",
    "src/crypto/internal/fips140/fips140.go",
    "src/crypto/internal/fips140/hkdf/cast.go",
    "src/crypto/internal/fips140/hkdf/hkdf.go",
    "src/crypto/internal/fips140/hmac/cast.go",
    "src/crypto/internal/fips140/hmac/hmac.go",
    "src/crypto/internal/fips140/indicator.go",
    "src/crypto/internal/fips140/mldsa/cast.go",
    "src/crypto/internal/fips140/mldsa/field.go",
    "src/crypto/internal/fips140/mldsa/mldsa.go",
    "src/crypto/internal/fips140/mldsa/semiexpanded.go",
    "src/crypto/internal/fips140/mlkem/cast.go",
    "src/crypto/internal/fips140/mlkem/field.go",
    "src/crypto/internal/fips140/mlkem/mlkem768.go",
    "src/crypto/internal/fips140/nistec/fiat/cast.go",
    "src/crypto/internal/fips140/nistec/nistec.go",
    "src/crypto/internal/fips140/nistec/p224_sqrt.go",
    "src/crypto/internal/fips140/nistec/p256.go",
    "src/crypto/internal/fips140/nistec/p256_asm.go",
    "src/crypto/internal/fips140/nistec/p256_ordinv.go",
    "src/crypto/internal/fips140/nistec/p256_table.go",
    "src/crypto/internal/fips140/notasan.go",
    "src/crypto/internal/fips140/notboring.go",
    "src/crypto/internal/fips140/notpurego.go",
    "src/crypto/internal/fips140/pbkdf2/cast.go",
    "src/crypto/internal/fips140/pbkdf2/pbkdf2.go",
    "src/crypto/internal/fips140/purego.go",
    "src/crypto/internal/fips140/rsa/cast.go",
    "src/crypto/internal/fips140/rsa/keygen.go",
    "src/crypto/internal/fips140/rsa/largeexponent.go",
    "src/crypto/internal/fips140/rsa/pkcs1v15.go",
    "src/crypto/internal/fips140/rsa/pkcs1v22.go",
    "src/crypto/internal/fips140/rsa/rsa.go",
    "src/crypto/internal/fips140/sha256/cast.go",
    "src/crypto/internal/fips140/sha256/sha256.go",
    "src/crypto/internal/fips140/sha256/sha256block.go",
    "src/crypto/internal/fips140/sha256/sha256block_amd64.go",
    "src/crypto/internal/fips140/sha256/sha256block_arm64.go",
    "src/crypto/internal/fips140/sha256/sha256block_asm.go",
    "src/crypto/internal/fips140/sha256/sha256block_noasm.go",
    "src/crypto/internal/fips140/sha256/sha256block_ppc64x.go",
    "src/crypto/internal/fips140/sha256/sha256block_s390x.go",
    "src/crypto/internal/fips140/sha3/cast.go",
    "src/crypto/internal/fips140/sha3/hashes.go",
    "src/crypto/internal/fips140/sha3/keccakf.go",
    "src/crypto/internal/fips140/sha3/sha3.go",
    "src/crypto/internal/fips140/sha3/sha3_amd64.go",
    "src/crypto/internal/fips140/sha3/sha3_arm64.go",
    "src/crypto/internal/fips140/sha3/sha3_noasm.go",
    "src/crypto/internal/fips140/sha3/sha3_s390x.go",
    "src/crypto/internal/fips140/sha3/shake.go",
    "src/crypto/internal/fips140/sha512/cast.go",
    "src/crypto/internal/fips140/sha512/sha512.go",
    "src/crypto/internal/fips140/sha512/sha512block.go",
    "src/crypto/internal/fips140/sha512/sha512block_amd64.go",
    "src/crypto/internal/fips140/sha512/sha512block_arm64.go",
    "src/crypto/internal/fips140/sha512/sha512block_asm.go",
    "src/crypto/internal/fips140/sha512/sha512block_noasm.go",
    "src/crypto/internal/fips140/sha512/sha512block_ppc64x.go",
    "src/crypto/internal/fips140/sha512/sha512block_s390x.go",
    "src/crypto/internal/fips140/ssh/kdf.go",
    "src/crypto/internal/fips140/subtle/constant_time.go",
    "src/crypto/internal/fips140/subtle/xor.go",
    "src/crypto/internal/fips140/subtle/xor_asm.go",
    "src/crypto/internal/fips140/subtle/xor_generic.go",
    "src/crypto/internal/fips140/subtle/xor_loong64.go",
    "src/crypto/internal/fips140/subtle/xor_mipsx.go",
    "src/crypto/internal/fips140/subtle/xor_riscv64.go",
    "src/crypto/internal/fips140/tls12/cast.go",
    "src/crypto/internal/fips140/tls12/tls12.go",
    "src/crypto/internal/fips140/tls13/cast.go",
    "src/crypto/internal/fips140/tls13/tls13.go",
    "src/crypto/cipher/cbc.go",
    "src/crypto/cipher/cfb.go",
    "src/crypto/cipher/cipher.go",
    "src/crypto/cipher/ctr.go",
    "src/crypto/cipher/gcm.go",
    "src/crypto/cipher/io.go",
    "src/crypto/cipher/ofb.go",
    "src/crypto/aes/aes.go",
    "src/crypto/hmac/hmac.go",
    "src/crypto/sha256/sha256.go",
    "src/crypto/sha512/sha512.go",
    "src/crypto/sha3/sha3.go",
    "src/crypto/subtle/constant_time.go",
    "src/crypto/subtle/dit.go",
    "src/crypto/subtle/xor.go",
    "src/crypto/rand/rand.go",
    "src/crypto/rand/text.go",
    "src/crypto/rand/util.go",
    "src/crypto/mlkem/mlkem.go",
    "src/crypto/mldsa/mldsa.go",
    "src/crypto/mldsa/mldsa_fips140v1.0.go",
    "src/crypto/mldsa/mldsa_fips140v1.26.go",
    "src/crypto/hpke/aead.go",
    "src/crypto/hpke/aead_fips140v1.0.go",
    "src/crypto/hpke/aead_fips140v1.26.go",
    "src/crypto/hpke/hpke.go",
    "src/crypto/hpke/kdf.go",
    "src/crypto/hpke/kem.go",
    "src/crypto/hpke/pq.go",

    # Untrusted archive, document and template input
    "src/archive/zip/reader.go",
    "src/archive/zip/register.go",
    "src/archive/zip/struct.go",
    "src/archive/zip/writer.go",
    "src/archive/tar/common.go",
    "src/archive/tar/format.go",
    "src/archive/tar/reader.go",
    "src/archive/tar/stat_actime1.go",
    "src/archive/tar/stat_actime2.go",
    "src/archive/tar/stat_unix.go",
    "src/archive/tar/strconv.go",
    "src/archive/tar/writer.go",
    "src/compress/gzip/gunzip.go",
    "src/compress/gzip/gzip.go",
    "src/compress/flate/deflate.go",
    "src/compress/flate/deflatefast.go",
    "src/compress/flate/dict_decoder.go",
    "src/compress/flate/huffman_bit_writer.go",
    "src/compress/flate/huffman_code.go",
    "src/compress/flate/inflate.go",
    "src/compress/flate/level1.go",
    "src/compress/flate/level2.go",
    "src/compress/flate/level3.go",
    "src/compress/flate/level4.go",
    "src/compress/flate/level5.go",
    "src/compress/flate/level6.go",
    "src/compress/flate/load_store.go",
    "src/compress/flate/regmask_amd64.go",
    "src/compress/flate/regmask_other.go",
    "src/compress/flate/token.go",
    "src/compress/zlib/reader.go",
    "src/compress/zlib/writer.go",
    "src/encoding/json/decode.go",
    "src/encoding/json/encode.go",
    "src/encoding/json/fold.go",
    "src/encoding/json/indent.go",
    "src/encoding/json/internal/internal.go",
    "src/encoding/json/internal/jsonflags/flags.go",
    "src/encoding/json/internal/jsonopts/options.go",
    "src/encoding/json/internal/jsonopts/options_format.go",
    "src/encoding/json/internal/jsonwire/decode.go",
    "src/encoding/json/internal/jsonwire/encode.go",
    "src/encoding/json/internal/jsonwire/wire.go",
    "src/encoding/json/jsontext/decode.go",
    "src/encoding/json/jsontext/doc.go",
    "src/encoding/json/jsontext/encode.go",
    "src/encoding/json/jsontext/errors.go",
    "src/encoding/json/jsontext/export.go",
    "src/encoding/json/jsontext/options.go",
    "src/encoding/json/jsontext/pools.go",
    "src/encoding/json/jsontext/quote.go",
    "src/encoding/json/jsontext/state.go",
    "src/encoding/json/jsontext/token.go",
    "src/encoding/json/jsontext/value.go",
    "src/encoding/json/scanner.go",
    "src/encoding/json/stream.go",
    "src/encoding/json/tables.go",
    "src/encoding/json/tags.go",
    "src/encoding/json/v2/arshal.go",
    "src/encoding/json/v2/arshal_any.go",
    "src/encoding/json/v2/arshal_default.go",
    "src/encoding/json/v2/arshal_embedded.go",
    "src/encoding/json/v2/arshal_funcs.go",
    "src/encoding/json/v2/arshal_methods.go",
    "src/encoding/json/v2/arshal_time.go",
    "src/encoding/json/v2/doc.go",
    "src/encoding/json/v2/errors.go",
    "src/encoding/json/v2/fields.go",
    "src/encoding/json/v2/fold.go",
    "src/encoding/json/v2/intern.go",
    "src/encoding/json/v2/options.go",
    "src/encoding/json/v2_decode.go",
    "src/encoding/json/v2_encode.go",
    "src/encoding/json/v2_indent.go",
    "src/encoding/json/v2_inject.go",
    "src/encoding/json/v2_options.go",
    "src/encoding/json/v2_scanner.go",
    "src/encoding/json/v2_stream.go",
    "src/encoding/xml/marshal.go",
    "src/encoding/xml/read.go",
    "src/encoding/xml/typeinfo.go",
    "src/encoding/xml/xml.go",
    "src/encoding/gob/decode.go",
    "src/encoding/gob/decoder.go",
    "src/encoding/gob/doc.go",
    "src/encoding/gob/encode.go",
    "src/encoding/gob/encoder.go",
    "src/encoding/gob/error.go",
    "src/encoding/gob/type.go",
    "src/html/template/attr.go",
    "src/html/template/content.go",
    "src/html/template/context.go",
    "src/html/template/css.go",
    "src/html/template/doc.go",
    "src/html/template/error.go",
    "src/html/template/escape.go",
    "src/html/template/html.go",
    "src/html/template/js.go",
    "src/html/template/template.go",
    "src/html/template/transition.go",
    "src/html/template/url.go",
    "src/text/template/doc.go",
    "src/text/template/exec.go",
    "src/text/template/funcs.go",
    "src/text/template/helper.go",
    "src/text/template/option.go",
    "src/text/template/parse/lex.go",
    "src/text/template/parse/node.go",
    "src/text/template/parse/parse.go",
    "src/text/template/template.go",
    "src/mime/multipart/formdata.go",
    "src/mime/multipart/multipart.go",
    "src/mime/multipart/readmimeheader.go",
    "src/mime/multipart/writer.go",

    # Path and command boundaries
    "src/os/exec/exec.go",
    "src/os/exec/exec_plan9.go",
    "src/os/exec/exec_unix.go",
    "src/os/exec/exec_windows.go",
    "src/os/exec/lookpath.go",
    "src/os/exec/lp_plan9.go",
    "src/os/exec/lp_unix.go",
    "src/os/exec/lp_wasm.go",
    "src/os/exec/lp_windows.go",
    "src/path/filepath/match.go",
    "src/path/filepath/path.go",
    "src/path/filepath/path_plan9.go",
    "src/path/filepath/path_unix.go",
    "src/path/filepath/path_windows.go",
    "src/path/filepath/symlink.go",
    "src/path/filepath/symlink_plan9.go",
    "src/path/filepath/symlink_unix.go",
    "src/path/filepath/symlink_windows.go",
    "src/go/parser/interface.go",
    "src/go/parser/parser.go",
    "src/go/parser/resolver.go",
    "src/go/scanner/errors.go",
    "src/go/scanner/scanner.go",
    "src/cmd/compile/internal/syntax/branches.go",
    "src/cmd/compile/internal/syntax/dumper.go",
    "src/cmd/compile/internal/syntax/nodes.go",
    "src/cmd/compile/internal/syntax/parser.go",
    "src/cmd/compile/internal/syntax/pos.go",
    "src/cmd/compile/internal/syntax/positions.go",
    "src/cmd/compile/internal/syntax/printer.go",
    "src/cmd/compile/internal/syntax/scanner.go",
    "src/cmd/compile/internal/syntax/source.go",
    "src/cmd/compile/internal/syntax/syntax.go",
    "src/cmd/compile/internal/syntax/tokens.go",
    "src/cmd/compile/internal/syntax/type.go",
    "src/cmd/compile/internal/syntax/walk.go",
    "src/cmd/compile/internal/noder/codes.go",
    "src/cmd/compile/internal/noder/doc.go",
    "src/cmd/compile/internal/noder/dump.go",
    "src/cmd/compile/internal/noder/export.go",
    "src/cmd/compile/internal/noder/helpers.go",
    "src/cmd/compile/internal/noder/html.go",
    "src/cmd/compile/internal/noder/import.go",
    "src/cmd/compile/internal/noder/irgen.go",
    "src/cmd/compile/internal/noder/lex.go",
    "src/cmd/compile/internal/noder/linker.go",
    "src/cmd/compile/internal/noder/noder.go",
    "src/cmd/compile/internal/noder/posmap.go",
    "src/cmd/compile/internal/noder/quirks.go",
    "src/cmd/compile/internal/noder/reader.go",
    "src/cmd/compile/internal/noder/types.go",
    "src/cmd/compile/internal/noder/unified.go",
    "src/cmd/compile/internal/noder/writer.go",

]

target_scopes = [
    "Critical/High. An unauthenticated HTTP client sends crafted HTTP/1 requests through a Go server or reverse proxy; net/http and net/textproto disagree on message boundaries, headers, trailers, or connection reuse, allowing request smuggling, authentication bypass, or access to another user's response. Audit src/net/http, src/net/textproto, and src/net/url.",
    "Critical/High. An unauthenticated HTTP/2 client sends validly framed but ambiguous requests; net/http's protocol translation, header normalization, stream handling, or proxy forwarding changes the authority, path, or request boundary seen by upstream code, causing cross-user request or response confusion. Audit src/net/http and src/net/url.",
    "Critical/High. An unauthenticated client presents a crafted certificate chain to a Go mTLS server; x509 verification accepts an invalid identity, chain, constraint, or signature, granting another user's authenticated access. Audit src/crypto/tls, src/crypto/x509, and the crypto verification code they call.",
    "Critical/High. An unauthenticated TLS client negotiates a handshake that makes a Go server accept the wrong peer identity, reuse secrets across contexts, or derive incorrect traffic keys, exposing protected sessions. Audit src/crypto/tls and src/crypto/internal/fips140/tls12 and tls13.",
    "Critical/High. Attacker-controlled signatures, public keys, or ciphertexts accepted by a real application cause a Go crypto verifier or KEM to accept invalid proofs or disclose key material, bypassing authentication or message integrity. Audit src/crypto/rsa, ecdsa, ed25519, ecdh, mlkem, mldsa, and their internal/fips140 implementations.",
    "Critical/High. An attacker publishes a module version that a victim builds; cmd/go's module fetch, checksum database, go.sum, cache, VCS, or version-selection path accepts bytes under an incorrect identity or digest, or executes module code during go build, compromising the build without running the resulting program. Audit src/cmd/go/internal/modfetch, modload, vcs, web, auth, cache, and work.",
    "High. An unauthenticated request or stored attacker-supplied data reaches html/template, text/template, JSON, XML, gob, or multipart parsing in a realistic service; escaping or parser boundary confusion turns data into executable browser content, a forged security-sensitive value, or access to another user's data. Audit src/html/template, src/text/template, src/encoding, and src/mime/multipart; require a concrete application entry point.",
    "High. A victim processes an attacker-published archive or path through archive/zip, archive/tar, filepath, or cmd/go extraction; a validation and extraction mismatch writes outside the intended directory or causes build-time execution. Audit src/archive, src/path/filepath, and src/cmd/go/internal/modfetch.",
    "High. A victim builds attacker-published source without executing it; go/parser, cmd/compile, os/exec, or cmd/go turns source bytes or metadata into a command or unintended file operation during go build. Audit src/go/parser, src/go/scanner, src/cmd/compile/internal/syntax and noder, src/os/exec, and src/cmd/go/internal/work.",
    "Critical/High blind spot. Find a reachable trust-boundary mismatch absent from the categories above: the same unprivileged request, certificate, module, archive, or source bytes are validated in one Go path then interpreted differently in another, producing remote code execution, authentication or integrity bypass, secret disclosure, or cross-user data access. Name both paths and prove the victim workflow requires no attacker privilege.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """Generate focused Go security questions for one file and scope."""

    prompt = f"""Generate 40 to 80 distinct security questions for this exact Go target:
{target_file}

Rules:
- `File Name:` is the file to inspect; `Scope:` is the only impact to pursue. Use the actual Go package, function, and reachable call path.
- Attacker is unprivileged: they can send an unauthenticated request, present a certificate or TLS handshake, publish a dependency/archive/source file the victim normally consumes, or submit ordinary user data. They cannot control the victim host, environment, trusted application code, credentials, or a privileged account.
- Each question needs a realistic victim workflow, concrete attacker bytes, entry point, path through this file, broken security property, impact, and a small Go test or integration reproducer.
- Prioritize code execution where none is intended, authentication/integrity bypass, secret or cross-user data disclosure, and build compromise. Include a meaningful Medium-impact case when supported by the scope.
- Respect Go's security decisions: building malicious code may be in scope; running that code or go test on attacker tests is not. Reject bare malicious peer/node/server premises, attacker-controlled host settings, hypothetical application misuse, redirect-header stripping alone, and known exclusions.
- Do not create questions about unbounded memory, huge inputs, generic panic or CPU exhaustion. Do not inspect test, mock, generated, vendored third-party, docs, README, or config files as findings.
- Vary root causes. Verify current behavior and defenses instead of presuming a bug. If this file has no reachable path to the scope, output an empty list.

Output only valid Python, with no markdown:
questions = [
    "[File: {target_file}] [Function: exact Go function] Can ATTACKER_INPUT in VICTIM_WORKFLOW reach CALL_PATH and violate PROPERTY, causing IMPACT? Proof: Go test with concrete input and expected security assertion.",
]
"""
    return prompt


def audit_format(security_question: str) -> str:
    """Generate a focused Go security audit prompt."""

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Analyze only the question in production golang/go code and its stated impact. Attacker is unprivileged and controls only a realistic remote request, certificate/handshake, ordinary user data, or published module/archive/source the victim consumes.
- Trace attacker bytes through the exact Go entry point, calls, checks, and sink. Confirm a supported release or current source path and show why existing checks fail.
- Follow Go Security Policy and Security Decisions. Building untrusted code should be safe; executing it or attacker tests is expected. Reject attacker control of host, environment, trusted app code, privileged account, or credentials; bare malicious peer/node/server claims; speculative misuse; and redirect-header stripping alone.
- No unbounded-memory, generic size/CPU, test/mock/generated/vendored third-party/docs/config-only, or dependency-only findings. A panic matters only if Go's policy treats that concrete entry point as security relevant. Build output containing local file data is not a security issue by itself.
- Require an observable security effect and a minimal Go test or integration reproduction. Do not infer impact from a parser discrepancy alone.

## Output
If valid, output exactly:
### Title
[Bug statement] - ([File: file_path])
### Summary
[2-3 sentences]
### Finding Description
[Entry point, code path, root cause, failed check]
### Impact Explanation
[Concrete victim impact and Go PUBLIC/PRIVATE/URGENT track assessment]
### Likelihood Explanation
[Attacker capability and victim workflow]
### Recommendation
[Specific fix]
### Proof of Concept
[Minimal Go test and expected result]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """Validate a Go security claim against the live project policy."""

    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate this claim only. Read SECURITY.md, Researcher.Md if present, and go.dev/doc/security/policy and go.dev/doc/security/decisions. Do not invent another finding.
- Go uses PUBLIC, PRIVATE, and URGENT tracks, not fixed severity labels. Preserve a proven Medium-impact issue if it qualifies for Go's PUBLIC or PRIVATE track; prioritize stronger impacts without discarding valid lesser ones. Do not promise bounty eligibility.
- Require a realistic unprivileged entry point, exact in-scope production Go function and line, attacker-controlled input, victim workflow, broken security property, observable impact, and a minimal reproducible Go test.
- Test existing parsing, verification, error handling, and release behavior. Explain why they fail. Reject duplicates, already-fixed claims, hypothetical impact, and claims requiring attacker control of the host, environment, trusted code, credentials, or privileged account.
- Apply Go's decisions: building malicious source must not execute it; running it or attacker-controlled tests is expected. Redirect-header stripping alone is not a security issue. A server panic from a normal unprivileged request can qualify; a parser panic requires plausibly malicious input. Reject generic huge-input memory/CPU claims and bare malicious peer/node/server premises.
- Ignore test, mock, generated, vendored third-party, docs, README, and config-only issues. Distinguish Go-distribution flaws from application misuse or third-party bugs. Build output containing local file data alone is excluded.

## Output
If valid, output exactly:
Audit Report
## Title
[Clear vulnerability statement] - ([File: file_path])
## Summary
[2-3 sentences]
## Finding Description
[Exact path, root cause, proof existing checks fail]
## Impact Explanation
[Concrete impact and appropriate Go track; describe severity in context only]
## Likelihood Explanation
[Preconditions and repeatability]
## Recommendation
[Specific fix]
## Proof of Concept
[Minimal reproducible Go test and assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """Scan Go for a validated analog of an external report."""

    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Treat the report as a bug-class hint, never proof. Find the closest production golang/go path and prove its own root cause. Use only files in scope_files; inspect related production callers to establish reachability.
- Attacker is unprivileged: unauthenticated HTTP request, TLS handshake/certificate, ordinary user data, or published module/archive/source consumed by a normal victim workflow. No host, environment, trusted code, credential, privileged-account, malicious peer/node, or malicious server control as the premise.
- Match the primitive, not keywords: compare parsing and interpretation across HTTP front/backend, TLS identity and certificate verification, cryptographic acceptance, module checksum/cache/VCS trust, template escaping, archive extraction, and build-time handling of untrusted source. Name the exact Go entry point, functions, checks, and sink.
- Show a concrete security effect: unexpected code execution, authentication or integrity bypass, secret/cross-user disclosure, or material file write. A real server panic or plausible malicious-input parser panic can qualify under Go's policy; never ask about unbounded memory, huge input, or generic resource exhaustion.
- Verify defenses and supported release behavior. Apply Go Security Decisions: go build must not run malicious source; running built code or attacker tests is expected; redirect-header stripping and local data in build output alone are out of scope. Reject hypothetical application misuse, duplicates, already-fixed bugs, and test/mock/generated/vendored third-party/docs/config-only paths.
- Give one strongest, reproducible analog with a minimal Go test and expected assertion. If none passes, report none; do not stretch an analogy.

## Output (Strict)
If valid, output exactly:
### Title
[Clear vulnerability statement] - ([File: file_path])
### Summary
[2-3 sentences]
### Finding Description
[Attacker input -> Go entry point -> exact functions -> failed check -> sink]
### Impact Explanation
[Concrete impact and Go PUBLIC/PRIVATE/URGENT track assessment]
### Likelihood Explanation
[Victim workflow and attacker capability]
### Recommendation
[Specific fix]
### Proof of Concept
[Minimal Go test, input and assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
