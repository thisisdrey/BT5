import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 23
# todo: the path from https://github.com/WebKit/WebKit
SOURCE_REPO = "WebKit/WebKit"
# todo: the name of the repository
REPO_NAME = "WebKit"
run_number = os.environ.get('GITHUB_RUN_NUMBER') or os.environ.get('CI_PIPELINE_IID', '0')


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
    # JavaScriptCore JIT: type confusion / OOB from mis-speculated types in DFG/FTL/B3
    # =================================================================================
    "Source/JavaScriptCore/dfg/DFGByteCodeParser.cpp",
    "Source/JavaScriptCore/dfg/DFGAbstractInterpreterInlines.h",
    "Source/JavaScriptCore/dfg/DFGArgumentsEliminationPhase.cpp",
    "Source/JavaScriptCore/dfg/DFGFixupPhase.cpp",
    "Source/JavaScriptCore/dfg/DFGConstantFoldingPhase.cpp",
    "Source/JavaScriptCore/dfg/DFGIntegerRangeOptimizationPhase.cpp",
    "Source/JavaScriptCore/dfg/DFGSpeculativeJIT.cpp",
    "Source/JavaScriptCore/ftl/FTLLowerDFGToB3.cpp",
    "Source/JavaScriptCore/b3/B3ReduceStrength.cpp",
    "Source/JavaScriptCore/bytecode/CodeBlock.cpp",
    "Source/JavaScriptCore/runtime/CommonSlowPaths.cpp",

    # =================================================================================
    # JSC runtime objects/arrays/typed arrays: butterfly OOB, structure type confusion
    # =================================================================================
    "Source/JavaScriptCore/runtime/JSArray.cpp",
    "Source/JavaScriptCore/runtime/ArrayPrototype.cpp",
    "Source/JavaScriptCore/runtime/JSArrayBufferView.cpp",
    "Source/JavaScriptCore/runtime/JSTypedArrayViewPrototype.cpp",
    "Source/JavaScriptCore/runtime/JSGenericTypedArrayViewInlines.h",
    "Source/JavaScriptCore/runtime/Structure.cpp",
    "Source/JavaScriptCore/runtime/JSObject.cpp",
    "Source/JavaScriptCore/runtime/JSGlobalObject.cpp",

    # =================================================================================
    # JSC regexp & WebAssembly: OOB / corruption from crafted patterns and modules
    # =================================================================================
    "Source/JavaScriptCore/runtime/RegExp.cpp",
    "Source/JavaScriptCore/yarr/YarrInterpreter.cpp",
    "Source/JavaScriptCore/yarr/YarrJIT.cpp",
    "Source/JavaScriptCore/wasm/WasmFunctionParser.h",
    "Source/JavaScriptCore/wasm/WasmSectionParser.cpp",
    "Source/JavaScriptCore/wasm/WasmBBQJIT.cpp",

    # =================================================================================
    # WebCore HTML/XML/CSS parsing: renderer memory corruption from attacker markup
    # =================================================================================
    "Source/WebCore/html/parser/HTMLDocumentParser.cpp",
    "Source/WebCore/html/parser/HTMLTreeBuilder.cpp",
    "Source/WebCore/html/parser/HTMLTokenizer.cpp",
    "Source/WebCore/html/parser/HTMLConstructionSite.cpp",
    "Source/WebCore/xml/parser/XMLDocumentParser.cpp",
    "Source/WebCore/xml/XSLTProcessorLibxslt.cpp",
    "Source/WebCore/css/parser/CSSParser.cpp",
    "Source/WebCore/css/parser/CSSTokenizer.cpp",
    "Source/WebCore/css/parser/CSSSelectorParser.cpp",
    "Source/WebCore/css/parser/CSSPropertyParser.cpp",

    # =================================================================================
    # WebCore DOM lifecycle & editing: use-after-free from tree mutation / callbacks
    # =================================================================================
    "Source/WebCore/dom/ContainerNode.cpp",
    "Source/WebCore/dom/Node.cpp",
    "Source/WebCore/dom/Document.cpp",
    "Source/WebCore/dom/Range.cpp",
    "Source/WebCore/dom/Element.cpp",
    "Source/WebCore/dom/CharacterData.cpp",
    "Source/WebCore/dom/CustomElementReactionQueue.cpp",
    "Source/WebCore/editing/Editing.cpp",
    "Source/WebCore/editing/CompositeEditCommand.cpp",
    "Source/WebCore/editing/markup.cpp",

    # =================================================================================
    # WebCore rendering/layout & SVG: UAF / OOB driven by style and tree updates
    # =================================================================================
    "Source/WebCore/rendering/RenderBlockFlow.cpp",
    "Source/WebCore/rendering/RenderLayer.cpp",
    "Source/WebCore/rendering/RenderObject.cpp",
    "Source/WebCore/rendering/updating/RenderTreeBuilder.cpp",
    "Source/WebCore/svg/SVGUseElement.cpp",
    "Source/WebCore/svg/SVGElement.cpp",

    # =================================================================================
    # Bindings & structured clone: type confusion / OOB from script and postMessage
    # =================================================================================
    "Source/WebCore/bindings/js/SerializedScriptValue.cpp",
    "Source/WebCore/bindings/js/JSDOMConvertBufferSource.h",
    "Source/WebCore/bindings/js/JSCustomElementInterface.cpp",
    "Source/WebCore/dom/MessagePort.cpp",
    "Source/WebCore/dom/MessageEvent.cpp",

    # =================================================================================
    # Same-origin policy / CORS / origin: cross-origin theft & universal XSS boundary
    # =================================================================================
    "Source/WebCore/page/SecurityOrigin.cpp",
    "Source/WebCore/loader/CrossOriginAccessControl.cpp",
    "Source/WebCore/loader/DocumentThreadableLoader.cpp",
    "Source/WebCore/loader/CrossOriginPreflightChecker.cpp",
    "Source/WebCore/loader/cache/CachedResourceLoader.cpp",
    "Source/WebCore/loader/FrameLoader.cpp",
    "Source/WebCore/page/csp/ContentSecurityPolicy.cpp",
    "Source/WebCore/platform/network/ResourceResponseBase.cpp",

    # =================================================================================
    # WebKit IPC boundary: validation of WebContent-supplied messages (sandbox escape)
    # =================================================================================
    "Source/WebKit/Platform/IPC/Decoder.cpp",
    "Source/WebKit/Platform/IPC/ArgumentCoders.cpp",
    "Source/WebKit/Platform/IPC/Connection.cpp",
    "Source/WebKit/Platform/IPC/StreamServerConnection.cpp",
    "Source/WebKit/UIProcess/WebPageProxy.cpp",
    "Source/WebKit/NetworkProcess/NetworkResourceLoader.cpp",

    # =================================================================================
    # Network wire parsing: HTTP/WebSocket/fetch bytes from attacker-controlled servers
    # =================================================================================
    "Source/WebCore/platform/network/HTTPParsers.cpp",
    "Source/WebCore/platform/network/ParsedContentType.cpp",
    "Source/WebCore/Modules/websockets/WebSocketFrame.cpp",
    "Source/WebCore/Modules/fetch/FetchResponse.cpp",

    # =================================================================================
    # Storage endpoints reachable from a page: IndexedDB object/value handling
    # =================================================================================
    "Source/WebCore/Modules/indexeddb/IDBObjectStore.cpp",
    "Source/WebCore/Modules/indexeddb/server/MemoryObjectStore.cpp",

    # =================================================================================
    # GPU/WebGL/WebGPU/canvas: GPU-process memory corruption from renderer commands
    # =================================================================================
    "Source/WebCore/html/canvas/WebGLRenderingContextBase.cpp",
    "Source/WebCore/html/canvas/WebGL2RenderingContext.cpp",
    "Source/WebCore/html/canvas/CanvasRenderingContext2DBase.cpp",
    "Source/WebCore/Modules/webgpu/GPUBuffer.cpp",
    "Source/WebGPU/WebGPU/Buffer.mm",

    # =================================================================================
    # Media/image/audio decoders: memory corruption from crafted media bytes
    # =================================================================================
    "Source/WebCore/Modules/mediasource/SourceBuffer.cpp",
    "Source/WebCore/platform/graphics/cocoa/SourceBufferParserWebM.cpp",
    "Source/WebCore/platform/graphics/ImageDecoder.cpp",
    "Source/WebCore/platform/image-decoders/ScalableImageDecoder.cpp",
    "Source/WebCore/Modules/webcodecs/WebCodecsVideoDecoder.cpp",
    "Source/WebCore/html/track/WebVTTParser.cpp",
    "Source/WebCore/Modules/webaudio/AudioBuffer.cpp",
    "Source/WebCore/platform/graphics/filters/FEColorMatrix.cpp",
]


target_scopes = [
    "Critical. A memory-safety bug in the JavaScriptCore JIT is reached from ordinary attacker JavaScript because DFG/FTL/B3 speculation, abstract interpretation, or a strength-reduction/constant-folding phase models a value's type, length, or aliasing wrong, so generated code omits a bounds or type check: JavaScript on a page the victim merely visits produces an OOB read/write or type confusion in the WebContent process - the memory-corruption primitive that starts a Safari RCE chain.",
    "Critical. A JSC runtime object corrupts memory from script because JSArray/Butterfly growth, an ArrayPrototype fast path, a JSArrayBufferView/typed-array offset, or a Structure/JSObject transition trusts an attacker-chosen length, index, or type: a plain script call (array mutation, .sort/.fill, typed-array view, defineProperty) yields an out-of-bounds butterfly access or a wrong-type object in the WebContent process.",
    "Critical. A renderer-process memory-safety bug (OOB read/write, use-after-free, uninitialized read) is reached from attacker markup or script through the Blink-equivalent WebCore HTML/XML/CSS parser, DOM tree mutation, editing/markup, or the render tree, giving control of WebContent memory from a page the victim only loads - the first half of an RCE chain.",
    "Critical. A compromised or malicious WebContent process escapes the sandbox because a UIProcess, GPUProcess, or NetworkProcess IPC endpoint trusts renderer-supplied data: IPC::Decoder/ArgumentCoders under-validate a decoded object, identifier, or handle, or a WebPageProxy/NetworkResourceLoader message handler acts on an out-of-range index, a forged page/frame identifier, or a SharedMemory/attachment the renderer is not entitled to, reaching a capability outside the WebContent sandbox.",
    "Critical. The same-origin policy is bypassed (universal XSS / cross-origin theft) because SecurityOrigin::canAccess, CrossOriginAccessControl, DocumentThreadableLoader, or CachedResourceLoader admits a cross-origin DOM access, script, or resource read it must deny, or a FrameLoader navigation commits a document with an origin that does not match its true source, so script from the attacker's page reads or scripts a cross-origin document.",
    "Critical/High. Structured clone or DOM messaging corrupts memory or confuses types because SerializedScriptValue deserialization, a JSDOMConvertBufferSource conversion, or MessagePort/MessageEvent transfer mis-handles an attacker-crafted serialized blob or transferred object delivered via postMessage, history.state, or IndexedDB: a bad tag, length, or transferred ArrayBuffer produces an OOB read/write or a wrong-type object in the WebContent process.",
    "Critical. The GPU process is memory-corrupted from WebGL/WebGPU/canvas traffic a page issues because WebGLRenderingContextBase, WebGL2RenderingContext, GPUBuffer, or the WebGPU Buffer backend under-validates sizes, offsets, or object references in commands the renderer submits, producing an OOB access or use-after-free in a process that outlives the WebContent process.",
    "Critical/High. A media, image, audio, or track decoder corrupts memory from crafted bytes a page feeds through <video>/<audio>/MSE, <img>, WebCodecs, WebAudio, or <track>, because SourceBuffer, the WebM source-buffer parser, ImageDecoder/ScalableImageDecoder, WebCodecsVideoDecoder, WebVTTParser, or AudioBuffer mis-sizes a buffer or trusts an attacker length/count, giving an OOB read/write in the media pipeline.",
    "High. A cross-origin resource's bytes or headers leak, or wire-format parsing corrupts memory, because CrossOriginPreflightChecker/CrossOriginAccessControl skips a required preflight or misreads an allow-list, ResourceResponseBase/HTTPParsers/ParsedContentType mis-parses attacker-server headers, or WebSocketFrame/FetchResponse mishandles a frame length, letting the attacker's page read a cross-origin response or triggering a parser memory-safety bug.",
    "Critical/High blind spot. Remote web content or an untrusted WebContent process abuses an assumption WebKit never wrote down: a value validated in the renderer trusted as validated in the UI/GPU/Network process, an origin or URL re-derived after the check that authorized it, a rule enforced on one navigation/response/message path but not its redirect, prerender, fragment-navigation, blob-URL, or worker twin, a JIT invariant that holds before but not after a bailout or OSR exit, or an object whose lifetime is proven safe only inside a single call but reused across an IPC or event-loop reentry - yielding a sandbox escape, same-origin-policy bypass, cross-origin disclosure, or attacker-controlled memory corruption.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one WebKit target.

    ```
    target_file format:
    "'File Name: Source/WebCore/page/SecurityOrigin.cpp -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit questions for this exact WebKit target:

    {target_file}

    Project focus:
    WebKit is the browser engine behind Safari. Focus only on what remote web content or an untrusted WebContent (renderer) process reaches: the JavaScriptCore engine and JIT, the WebCore HTML/XML/CSS parsers and DOM/editing/render tree, V8-equivalent bindings and structured clone, the same-origin policy / CORS / origin machinery, the WebContent<->UIProcess/GPUProcess/NetworkProcess IPC boundary, HTTP/WebSocket/fetch wire parsing, IndexedDB endpoints, the WebGL/WebGPU/canvas surface, and media/image/audio decoders.

    Rules:
    * Treat `File Name:` as the exact file/component.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact C++/Objective-C++ symbols (function, method, class, field, IPC message) when possible.
    * Attacker is unprivileged only: remote web content the victim merely visits (HTML, CSS, JS, WASM, media, fonts, images, responses from the attacker's own servers, WebGL/WebGPU calls, and any IPC message script can drive), and - per WebKit's documented threat model - a compromised or fully malicious WebContent process sending arbitrary IPC across the sandbox boundary.
    * Attacker is NOT a local user, does NOT have OS/host/physical access, entitlements, non-default preferences or MDM/configuration profiles, an installed extension, an MITM/network position, or the victim's cooperation beyond loading a page and at most one click.
    * Out of scope, never ask about: bugs needing runtime preferences/experimental-feature flags, configuration profiles, or internal test hooks; WebKit extensions or app-embedder API misuse; MITM/TLS-only issues; local/physical access; social engineering; third-party libraries not built as WebKit; fingerprinting; and pure denial-of-service or crash-only-without-memory-safety (an unexploitable null-deref or resource-exhaustion tab crash is out of scope).
    * Ignore test files, mocks, fuzzers, benchmarks, docs, generated code (`*.serialization.in` output, bindings glue, `Derived*`), and build/config-only findings.
    * Every question must describe a real page, script call, markup, media/response payload, GPU command, or IPC message an attacker actually delivers through a valid entrypoint, and a concrete broken invariant. No generic "what if the input is huge" without a submitted payload and a corrupted object or crossed boundary.
    * Generate 40 to 80 high-signal questions.
    * At least 70% must target sandbox escape, same-origin-policy bypass / universal XSS, cross-origin information disclosure, or attacker-controlled memory corruption (OOB read/write, use-after-free, type confusion, uninitialized read) in the WebContent, GPU, Network, or UI process.
    * Every question must be testable by a C++ unit or layout/API test: a JSC test (jsc-stress/microbenchmark), a WebCore/Blink-equivalent layout or unit test, a TestWebKitAPI IPC/API test, a NetworkProcess test, a WebGL/WebGPU conformance test, or a media/image decoder test.
    * Avoid generic checklist questions and repeated root causes.

    Core invariants:
    * Sandbox integrity: the UIProcess/GPUProcess/NetworkProcess never trusts WebContent-supplied data; every decoded IPC field, object identifier, handle, and shared-memory attachment is validated before use.
    * Same-origin policy: content from one origin cannot read or script another origin's document or bytes unless CORS/postMessage explicitly permits it (SecurityOrigin::canAccess and the CORS path enforce this).
    * Origin integrity: the origin computed for a document or response equals its true source; URL and origin parsing is unambiguous.
    * Memory safety: no attacker-controlled input produces an OOB access, use-after-free, type confusion, or uninitialized read in any process.
    * JIT correctness: DFG/FTL/B3 speculation and abstract interpretation match the true runtime type, length, and effects; generated code never drops a required bounds or type check.

    Each question must include:
    1. target function/method;
    2. attacker action (a concrete page, script/DOM call, markup, media or network payload, GPU command, or IPC message: message name, fields, arguments);
    3. preconditions (frames, origins, process, contracts, handles the attacker controls);
    4. execution sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: symbol_or_method] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger EXECUTION_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: C++ JSC/WebCore/TestWebKitAPI/network/GPU/media test PARAMETERS and assert SANDBOX_INTEGRITY, SAME_ORIGIN_POLICY, ORIGIN_INTEGRITY, MEMORY_SAFETY, or JIT_CORRECTNESS.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a focused WebKit exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: remote web content the victim visits (HTML/CSS/JS/WASM, media, fonts, responses from the attacker's servers, WebGL/WebGPU calls, and IPC script can drive), or - per WebKit's threat model - a compromised/malicious WebContent process sending arbitrary IPC. No OS/host/physical access, entitlements, non-default preferences or configuration profiles, extensions, MITM position, or victim cooperation beyond loading a page and at most one click.
- Reject paths needing experimental-feature flags, configuration profiles, internal test hooks, WebKit extensions or embedder-API misuse, MITM/TLS-only, local access, social engineering, or third-party libraries not built as WebKit.
- Reject fingerprinting, missing-hardening, best-practice, and pure denial-of-service or crash-only-without-memory-safety findings (an unexploitable null-deref or resource-exhaustion tab crash is out of scope).
- Reject test/mock/fuzzer/docs/generated/build-config-only findings.
- Reject generic resource-growth claims with no concrete submitted payload and no crossed boundary or corrupted object.
- The Apple Security Bounty rewards Critical, High and Medium WebKit issues. Focus on real security impact: WebContent sandbox escape, remote code execution, same-origin-policy bypass / universal XSS, cross-origin information disclosure, attacker-controlled memory corruption (OOB read/write, use-after-free, type confusion), or a convincing address-bar/security-UI spoof.

## Validate
- Trace the exact reachable path from the attacker's page, script/DOM call, markup, media or network payload, GPU command, or IPC message into the affected function.
- Check whether existing checks already stop it: SecurityOrigin::canAccess and the CORS/preflight path, navigation origin/URL validation, IPC::Decoder/ArgumentCoders and message-handler validation, JIT speculation/bounds checks, or command-buffer/parser bounds checks.
- Confirm the path is reachable in a default release build with the sandbox on and no experimental flags.
- Accept only a concrete sandbox escape, SOP bypass, cross-origin disclosure, memory-corruption primitive, or security-UI spoof - not an unexploitable crash.
- Require exact file/function support and a reproducible C++ JSC, WebCore layout/unit, TestWebKitAPI IPC, NetworkProcess, WebGL/WebGPU, or media-decoder proof.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker inputs, exploit flow, and why checks fail]

### Impact Explanation
[Concrete scoped impact and severity: Critical (WebContent sandbox escape / RCE, or SOP bypass giving broad cross-origin control), High (cross-origin data disclosure, renderer/GPU memory corruption, or a universal-XSS primitive), or Medium (narrow info leak, deterministic origin-confusion/address-bar spoof, or a constrained memory-safety issue)]

### Likelihood Explanation
[Preconditions, frames/origins/process state needed, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[C++ JSC/WebCore/TestWebKitAPI/network/GPU/media test plan with expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for WebKit.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope production repo context only. Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only analogs remote web content or an untrusted WebContent process can reach: the JavaScriptCore engine/JIT, WebCore HTML/XML/CSS/DOM/editing/render, bindings and structured clone, the same-origin/CORS/origin machinery, the WebContent<->UIProcess/GPUProcess/NetworkProcess IPC boundary, HTTP/WebSocket/fetch parsing, IndexedDB endpoints, the WebGL/WebGPU/canvas surface, or media/image/audio decoders.
- Reject paths needing experimental flags, configuration profiles, extensions, embedder-API misuse, MITM/TLS-only, local/physical access, social engineering, or third-party libraries not built as WebKit.
- Reject fingerprinting, best-practice, mocked-only paths, and pure denial-of-service or crash-only-without-memory-safety analogs.
- Medium, High and Critical only; no low, informational, or resource-only analogs.

## Validate
- Map the bug class to the strongest reachable WebKit path from a single page, script/DOM call, markup, media or network payload, GPU command, or IPC message.
- Prove root cause with exact file/function support.
- Accept only a concrete sandbox escape, SOP bypass / universal XSS, cross-origin disclosure, attacker-controlled memory corruption, or convincing security-UI spoof.

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


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for WebKit security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes, and apply the Apple Security Bounty (WebKit/Safari) severity model, which rewards Critical, High and Medium.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject low, informational, best-practice, hardening, and speculative reports.
- Reject paths needing experimental-feature flags, runtime preferences, internal test hooks, configuration/MDM profiles, an installed extension, embedder-API misuse, an MITM/network position, local or physical access, victim social engineering, or the victim's cooperation beyond loading a page and at most one click.
- Reject fingerprinting, SSL/TLS best-practice, missing-header, and third-party (not-built-as-WebKit) findings.
- Reject pure denial-of-service and crash-only-without-memory-safety reports (an unexploitable null-deref or resource-exhaustion tab crash is out of scope); a crash is in scope only when it is a controllable memory-safety bug.
- Reject docs/style, generated-file, and test/mock/build-config-only issues.
- A valid report must be triggerable by remote web content or an untrusted WebContent process, unless the claim proves escalation from that starting point.
- The final impact must map to an in-scope category: Critical - WebContent sandbox escape or remote code execution reachable from a web page, or a same-origin-policy bypass giving broad cross-origin read/write; High - cross-origin information disclosure, attacker-controlled memory corruption (OOB read/write, use-after-free, type confusion) in the WebContent, GPU, Network, or UI process, or a universal-XSS primitive; Medium - a narrow information leak, a deterministic origin-confusion or address-bar/security-UI spoof, or a constrained memory-safety issue with limited attacker control.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and a broken sandbox-integrity, same-origin-policy, origin-integrity, memory-safety, or JIT-correctness invariant.
3. Reachable exploit path: preconditions (attacker-controlled frames, origins, process state, handles) -> page/script/DOM call, markup, media or network payload, GPU command, or IPC message -> trigger -> bad result.
4. Existing checks reviewed and shown insufficient: SecurityOrigin::canAccess and CORS/preflight, navigation origin/URL validation, IPC::Decoder/ArgumentCoders and message-handler validation, JIT speculation/bounds checks, and command-buffer/parser bounds checks.
5. Concrete in-scope Critical/High/Medium impact with realistic likelihood.
6. Reproducible proof path: a C++ JSC test, WebCore layout/unit test, TestWebKitAPI IPC/API test, NetworkProcess test, WebGL/WebGPU test, media/image-decoder test, or exact steps in a default release build.
7. No obvious rejection reason from SECURITY.md, known issues, privilege assumptions, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can remote web content or an untrusted WebContent process trigger this without flags, profiles, extensions, MITM, local access, or victim cooperation beyond a page load?
- Does the code actually behave as claimed in a default release build with the sandbox on?
- Is the impact caused by this code, not by a third-party component or a mere unexploitable crash?
- Is the escape, SOP bypass, disclosure, corruption, or spoof concrete rather than hypothetical?
- Would an Apple Product Security triager accept the proof-of-concept?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact, severity rationale, and Apple Security Bounty impact category]

## Likelihood Explanation
[Attacker capability, frames/origins/process state required, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or C++ JSC/WebCore/TestWebKitAPI/network/GPU/media test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt
