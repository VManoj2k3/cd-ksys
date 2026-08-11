"""Phase 2 RAG violation-detection scenarios on TWO REAL guideline PDFs.

    python -m tests.test_rag_scenarios

Exercises the whole detection path end to end on genuine PDF bytes:
pypdf extraction -> chunking -> hash embedding -> disk store -> retrieval ->
guideline judge -> anchor + rule-index validation -> adversarial verify ->
citation (rule text + source PDF + page) -> fix engine.

The judge's raw semantic decision is scripted (no GPU in CI) but FAITHFUL: it
flags a rule only when (a) retrieval actually surfaced that rule AND (b) the
submitted code truly contains the offending pattern. So "compliant code stays
silent" and "unselected PDF -> no finding" are real outcomes of retrieval and
anchoring — not of the script choosing to stay quiet. Real-*model* judgment is
validated separately on GPU (tests/rag_smoke.py).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="rag-scenarios-")
_PORT = 8936
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OVERLAY = f"""
logging: {{level: warning}}
server: {{host: 127.0.0.1}}
auth: {{mode: token}}
llm:
  base_url: http://127.0.0.1:{_PORT}/v1
  mock: false
  timeout_seconds: 30
  fix: {{max_retries: 0}}
audit: {{enabled: false}}
rag:
  enabled: true
  data_dir: {_TMP}/data
  embedder: {{backend: hash, hash_dim: 256}}
"""
_ov = os.path.join(_TMP, "ov.yaml")
open(_ov, "w").write(_OVERLAY)
os.environ["KOOSYS_CONFIG"] = os.path.join(_REPO, "config.yaml")
os.environ["KOOSYS_CONFIG_OVERLAY"] = _ov
os.environ["KOOSYS_SESSION_SECRET"] = "s" * 64
os.environ["KOOSYS_SHARED_TOKEN"] = "tok"

try:
    import pypdf  # noqa: F401
except Exception as _exc:  # noqa: BLE001 — env without a working pypdf: skip
    print(f"SKIP: pypdf unavailable ({type(_exc).__name__}); real-PDF ingest "
          f"needs it. This runs in CI where pypdf is installed.")
    raise SystemExit(0) from None

from backend.models import Layer, ReviewJob  # noqa: E402
from backend.orchestrator import run_review  # noqa: E402
from backend.rag import ingest, store  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402

FAIL: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'PASS ' if ok else 'FAIL '} {label}")
    if not ok:
        FAIL.append(label)


_LOOP = asyncio.new_event_loop()


def review(code: str, lang: str, user: str, rag: bool = False,
           cids: list[str] | None = None) -> ReviewJob:
    job = ReviewJob(job_id="t", filename=f"m.{lang}", code=code,
                    requested_language=lang, user=user, rag_enabled=rag,
                    collection_ids=cids or [])
    _LOOP.run_until_complete(run_review(job))
    assert job.state == "done", job.error
    return job


def gviol(job: ReviewJob) -> list:
    return [v for v in job.violations if v.layer == Layer.GUIDELINE]


def confirm(_p: str) -> dict:
    return {"confirmed": True, "reason": "matches the cited rule"}


# ---------------------------------------------------------------- PDF builder
def make_pdf(pages: list[str]) -> bytes:
    """Multi-page PDF with a byte-accurate xref (parses on strict pypdf 5.x)."""
    n_pages = len(pages)
    page_nums = [3 + 2 * i for i in range(n_pages)]
    content_nums = [4 + 2 * i for i in range(n_pages)]
    font_obj = 3 + 2 * n_pages
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode(),
    ]
    for i, text in enumerate(pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_nums[i]} 0 R /Resources << /Font << /F1 "
            f"{font_obj} 0 R >> >> >>".encode())
        body, y = [], 740
        for para in text.split("\n"):
            esc = para.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            body.append(f"BT /F1 10 Tf 54 {y} Td ({esc}) Tj ET")
            y -= 16
        stream = "\n".join(body).encode("latin-1")
        objs.append(b"<< /Length " + str(len(stream)).encode()
                    + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_pos = len(out)
    total = len(objs) + 1
    out += f"xref\n0 {total}\n".encode("latin-1") + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Root 1 0 R /Size {total} >>\n"
            f"startxref\n{xref_pos}\n").encode("latin-1")
    return bytes(out) + b"%%EOF"


# two genuine coding-standard PDFs (the C one is two pages)
C_PDF = make_pdf([
    "ACME Secure C Coding Standard v1.2\n"
    "Rule SEC-01: The gets function must never be used because it cannot bound "
    "its input and leads to buffer overflows. Use fgets with an explicit size.\n"
    "Rule SEC-02: Do not use strcpy or strcat. Prefer strncpy or snprintf with "
    "an explicit size bound.",
    "Rule MEM-03: Every pointer returned by malloc must be checked against NULL "
    "before it is dereferenced.\n"
    "Rule CTRL-04: The goto statement is forbidden. Use structured control flow "
    "such as loops and helper functions instead.",
])
PY_PDF = make_pdf([
    "ACME Python Engineering Guidelines\n"
    "Rule PYLOG-01: Do not use print in library modules. Use the logging module "
    "so that output can be controlled and routed.\n"
    "Rule PYSEC-02: Never call eval or exec on external or untrusted input; it "
    "enables code injection.\n"
    "Rule PYDOC-03: Every TODO comment must reference a tracking ticket, for "
    "example TODO PROJ-123.",
])
JAVA_PDF = make_pdf([
    "ACME Java Engineering Guidelines\n"
    "Rule JAVA-01: Do not call System.out.println in production code. Route "
    "output through a logging framework such as SLF4J.\n"
    "Rule JAVA-02: Never catch the generic Exception type; catch the specific "
    "exceptions you can actually handle.\n"
    "Rule JAVA-03: Do not compare strings with the == operator; use the equals "
    "method instead.",
])
TS_PDF = make_pdf([
    "ACME TypeScript Engineering Guidelines\n"
    "Rule TS-01: Do not use the any type. Declare precise types so the compiler "
    "can check them.\n"
    "Rule TS-02: Always use strict equality (===) and never the loose == "
    "operator.\n"
    "Rule TS-03: Do not leave console.log calls in committed code; use the "
    "project logger.",
])
CPP_PDF = make_pdf([
    "ACME C++ Engineering Guidelines\n"
    "Rule CPP-01: Do not put using namespace std at global or namespace scope in "
    "headers or shared code.\n"
    "Rule CPP-02: Use nullptr for null pointers, never NULL or the literal 0.\n"
    "Rule CPP-03: Do not manage memory with raw new and delete; use smart "
    "pointers such as std::make_unique.",
])

USER = "eng"
c_cid = store.create_collection(USER, "Secure C Standard")["id"]
py_cid = store.create_collection(USER, "Python Guidelines")["id"]
java_cid = store.create_collection(USER, "Java Guidelines")["id"]
ts_cid = store.create_collection(USER, "TypeScript Guidelines")["id"]
cpp_cid = store.create_collection(USER, "C++ Guidelines")["id"]
nc = ingest.ingest_pdf(USER, c_cid, "ACME_Secure_C_Standard.pdf", C_PDF)
npy = ingest.ingest_pdf(USER, py_cid, "ACME_Python_Guidelines.pdf", PY_PDF)
nj = ingest.ingest_pdf(USER, java_cid, "ACME_Java_Guidelines.pdf", JAVA_PDF)
nt = ingest.ingest_pdf(USER, ts_cid, "ACME_TypeScript_Guidelines.pdf", TS_PDF)
ncpp = ingest.ingest_pdf(USER, cpp_cid, "ACME_Cpp_Guidelines.pdf", CPP_PDF)
print(f"ingested 5 language PDFs -> C:{nc} Py:{npy} Java:{nj} TS:{nt} C++:{ncpp} chunk(s)")
if min(nc, npy, nj, nt, ncpp) < 1:
    print("FAIL: PDF ingestion produced no chunks")
    sys.exit(1)


# ---------------------------------------------------------------- faithful judge
RULE_RE = re.compile(r"\[R(\d+)\]\s*\(from\s+([^)]*)\)\s*(.*)")
LINE_RE = re.compile(r"^\s*(\d+)\|\s?(.*)$")
TODO_RE = re.compile(r"#\s*todo", re.I)
TICKET_RE = re.compile(r"[A-Z]{2,}-\d+")
DETECTORS = [
    (re.compile(r"\bgets\s*\("), "gets", "Uses gets() — unbounded input (SEC-01)."),
    (re.compile(r"\bstrcpy\s*\("), "strcpy", "Uses strcpy() — no bounds (SEC-02)."),
    (re.compile(r"\bgoto\b"), "goto", "Uses goto — forbidden (CTRL-04)."),
    (re.compile(r"\bprint\s*\("), "print", "print() in a module — use logging (PYLOG-01)."),
    (re.compile(r"\beval\s*\("), "eval", "eval() on input — injection risk (PYSEC-02)."),
    (re.compile(r"System\.out\.print"), "system.out", "System.out.println (JAVA-01)."),
    (re.compile(r"catch\s*\(\s*Exception\b"), "generic exception", "catches Exception (JAVA-02)."),
    (re.compile(r"(?<![=!<>])==(?!=)"), "==", "loose == comparison (JAVA-03/TS-02)."),
    (re.compile(r":\s*any\b"), "any type", "uses the any type (TS-01)."),
    (re.compile(r"console\.log"), "console.log", "console.log left in (TS-03)."),
    (re.compile(r"using\s+namespace\s+std"), "using namespace std", "using namespace std (CPP-01)."),
    (re.compile(r"\bnew\b"), "raw new", "raw new (CPP-03)."),
    (re.compile(r"\bNULL\b"), "nullptr", "NULL not nullptr (CPP-02)."),
]


def faithful_judge(prompt: str) -> dict:
    rules = [(int(m.group(1)), m.group(2).strip(), m.group(3).strip())
             for m in RULE_RE.finditer(prompt)]

    def idx_for(keyword: str):
        for idx, _src, text in rules:
            if keyword.lower() in text.lower():
                return idx
        return None

    code = [(int(m.group(1)), m.group(2))
            for m in (LINE_RE.match(ln) for ln in prompt.splitlines()) if m]
    out = []
    for lineno, text in code:
        for pat, keyword, msg in DETECTORS:
            ri = idx_for(keyword)
            if pat.search(text) and ri:
                out.append({"line": lineno, "snippet": text.strip(),
                            "rule_index": ri, "severity": "high", "message": msg})
        ti = idx_for("todo")
        if TODO_RE.search(text) and not TICKET_RE.search(text) and ti:
            out.append({"line": lineno, "snippet": text.strip(), "rule_index": ti,
                        "severity": "low", "message": "TODO without a ticket (PYDOC-03)."})
    return {"violations": out}


NO_REVIEW = {"review": lambda p: {"violations": []}}  # isolate the Phase-2 signal

C_BAD = ('#include <stdio.h>\nint main(void){\n  char buf[64];\n'
         '  gets(buf);\n  char d[8];\n  strcpy(d, buf);\n  return 0;\n}\n')

fake = FakeLLM(_PORT)
fake.start()
try:
    print("== S1: C code violates SEC-01(gets)+SEC-02(strcpy) -> detected + cited ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    job = review(C_BAD, "c", USER, rag=True, cids=[c_cid])
    gv = gviol(job)
    check(len(gv) == 2, f"two guideline violations ({len(gv)})")
    check(all(v.citation and v.citation.source == "ACME_Secure_C_Standard.pdf" for v in gv),
          "both cite the C standard PDF")
    check(any("gets" in v.citation.quote.lower() for v in gv),
          "citation quotes the actual SEC-01 rule text")
    check(all(v.line in (4, 6) for v in gv), f"anchored to real lines ({[v.line for v in gv]})")

    print("== S2: compliant C (fgets, no goto) -> silent ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    clean_c = ('#include <stdio.h>\nint main(void){\n  char buf[64];\n'
               '  fgets(buf, sizeof(buf), stdin);\n  return 0;\n}\n')
    job = review(clean_c, "c", USER, rag=True, cids=[c_cid])
    check(gviol(job) == [], f"no guideline findings on compliant C ({len(gviol(job))})")

    print("== S3: goto violation cites CTRL-04 from PAGE 2 (page number in citation) ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    goto_c = 'int f(int x){\n  if(x)\n    goto end;\n  end:\n  return x;\n}\n'
    job = review(goto_c, "c", USER, rag=True, cids=[c_cid])
    gv = gviol(job)
    check(len(gv) == 1 and "goto" in gv[0].citation.quote.lower(), "goto flagged, CTRL-04 cited")
    check(bool(gv) and gv[0].citation.page == 2,
          f"citation page == 2 ({gv[0].citation.page if gv else None})")

    print("== S4: Python print+eval+bare-TODO -> 3 cited to the Python PDF ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    py = ('def run(cmd):\n    print("running")\n    return eval(cmd)\n'
          '# TODO: harden this later\n')
    job = review(py, "py", USER, rag=True, cids=[py_cid])
    gv = gviol(job)
    check(len(gv) == 3, f"three guideline violations ({len(gv)})")
    check(all(v.citation.source == "ACME_Python_Guidelines.pdf" for v in gv),
          "all cite the Python guidelines PDF")

    print("== S5: compliant Python (logging, ticketed TODO) -> silent ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    clean_py = ('import logging\nlog = logging.getLogger(__name__)\n'
                'def run(cmd):\n    log.info("running")\n'
                '    # TODO(PROJ-123): support dry-run\n    return 0\n')
    job = review(clean_py, "py", USER, rag=True, cids=[py_cid])
    check(gviol(job) == [], f"no guideline findings on compliant Python ({len(gviol(job))})")

    print("== S6: both collections selected -> C finding cites the C PDF only ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    job = review(C_BAD, "c", USER, rag=True, cids=[c_cid, py_cid])
    gv = gviol(job)
    check(bool(gv) and all(v.citation.source == "ACME_Secure_C_Standard.pdf" for v in gv),
          "with both selected, C findings still cite the C PDF")

    print("== S7: retrieval faithfulness — C code but only the PYTHON PDF selected -> silent ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    job = review(C_BAD, "c", USER, rag=True, cids=[py_cid])
    check(gviol(job) == [],
          f"no finding when the relevant rule isn't in the selected PDF ({len(gviol(job))})")

    print("== S8: toggle OFF -> pure Phase 1, guideline layer not run ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    job = review(C_BAD, "c", USER, rag=False, cids=[c_cid])
    check(gviol(job) == [], "no guideline findings when toggle off")
    check(not any(ls.name == "guideline" for ls in job.layers), "guideline layer not run when off")

    print("== S9: anti-FP — judge cites a snippet not in the code -> dropped by anchor ==")
    fake.reset(guideline=lambda p: {"violations": [
        {"line": 4, "snippet": "system(rm_minus_rf_not_in_code)", "rule_index": 1,
         "severity": "high", "message": "hallucinated"}]},
        guideline_verify=confirm, **NO_REVIEW)
    job = review(C_BAD, "c", USER, rag=True, cids=[c_cid])
    check(gviol(job) == [], "hallucinated-snippet finding rejected by the anchor gate")

    print("== S10: anti-FP — out-of-range rule_index -> dropped ==")
    fake.reset(guideline=lambda p: {"violations": [
        {"line": 4, "snippet": "gets(buf);", "rule_index": 99,
         "severity": "high", "message": "bad index"}]},
        guideline_verify=confirm, **NO_REVIEW)
    job = review(C_BAD, "c", USER, rag=True, cids=[c_cid])
    check(gviol(job) == [], "finding citing a non-existent rule index rejected")

    print("== S11: anti-FP — adversarial verifier vetoes an unconfirmed finding ==")
    fake.reset(guideline=faithful_judge,
               guideline_verify=lambda p: {"confirmed": False, "reason": "rule does not apply"},
               **NO_REVIEW)
    job = review(C_BAD, "c", USER, rag=True, cids=[c_cid])
    check(gviol(job) == [], "verifier drops findings it cannot confirm against the rule")

    print("== S12: inline fix offered + validated for the eval() violation ==")

    def eval_only(p: str) -> dict:
        return {"violations": [x for x in faithful_judge(p)["violations"]
                               if "eval" in x["message"].lower()]}

    fake.reset(guideline=eval_only, guideline_verify=confirm,
               fix=lambda p: {"start_line": 3, "end_line": 3,
                              "replacement": "    return ast.literal_eval(cmd)"},
               fix_verify=lambda p: {"approved": True, "reason": "safer, equivalent for literals"},
               **NO_REVIEW)
    py2 = "import ast\ndef run(cmd):\n    return eval(cmd)\n"
    job = review(py2, "py", USER, rag=True, cids=[py_cid])
    gv = gviol(job)
    check(bool(gv), "eval violation detected")
    check(bool(gv) and gv[0].fix is not None and gv[0].fix.validated,
          f"inline fix present + validated "
          f"({gv[0].fix.replacement.strip() if gv and gv[0].fix else None})")

    # ---- S13-S15: the other three languages (Java, TypeScript, C++) ----
    JAVA_BAD = ('public class Svc {\n  String check(String a, String b) {\n'
                '    System.out.println("checking");\n'
                '    if (a == b) { return "same"; }\n'
                '    try { return a.trim(); } catch (Exception e) { return ""; }\n'
                '  }\n}\n')
    JAVA_OK = ('import org.slf4j.Logger;\nimport org.slf4j.LoggerFactory;\n'
               'public class Svc {\n'
               '  private static final Logger log = LoggerFactory.getLogger(Svc.class);\n'
               '  String check(String a, String b) {\n    log.info("checking");\n'
               '    if (a.equals(b)) { return "same"; }\n'
               '    try { return a.trim(); } catch (NullPointerException e) { return ""; }\n'
               '  }\n}\n')
    TS_BAD = ('function parse(input: any): number {\n  console.log(input);\n'
              '  if (input == null) { return 0; }\n  return input.length;\n}\n')
    TS_OK = ('function parse(input: string | null): number {\n'
             '  if (input === null) { return 0; }\n  return input.length;\n}\n')
    CPP_BAD = ('#include <cstddef>\nusing namespace std;\n'
               'int* make() {\n  int* p = new int(5);\n'
               '  if (p == NULL) { return NULL; }\n  return p;\n}\n')
    CPP_OK = ('#include <memory>\nstd::unique_ptr<int> make() {\n'
              '  auto p = std::make_unique<int>(5);\n'
              '  if (p == nullptr) { return nullptr; }\n  return p;\n}\n')
    langs = [
        ("S13 Java", "java", java_cid, "ACME_Java_Guidelines.pdf", JAVA_BAD, JAVA_OK),
        ("S14 TypeScript", "ts", ts_cid, "ACME_TypeScript_Guidelines.pdf", TS_BAD, TS_OK),
        ("S15 C++", "cpp", cpp_cid, "ACME_Cpp_Guidelines.pdf", CPP_BAD, CPP_OK),
    ]
    for label, lang, lcid, src, bad, good in langs:
        print(f"== {label}: violating -> cited to its PDF; compliant -> silent ==")
        fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
        job = review(bad, lang, USER, rag=True, cids=[lcid])
        gv = gviol(job)
        check(len(gv) >= 2, f"{label} violations detected ({len(gv)})")
        check(bool(gv) and all(v.citation and v.citation.source == src for v in gv),
              f"{label} findings cite {src}")
        job = review(good, lang, USER, rag=True, cids=[lcid])
        check(gviol(job) == [], f"{label} compliant stays silent ({len(gviol(job))})")

    # ---- S16: all 5 collections selected at once -> correct attribution ----
    print("== S16: all 5 collections selected, TS bug -> cites the TS PDF only ==")
    fake.reset(guideline=faithful_judge, guideline_verify=confirm, **NO_REVIEW)
    job = review(TS_BAD, "ts", USER, rag=True,
                 cids=[c_cid, py_cid, java_cid, ts_cid, cpp_cid])
    gv = gviol(job)
    check(bool(gv) and all(v.citation.source == "ACME_TypeScript_Guidelines.pdf" for v in gv),
          f"TS bug cites the TS PDF even with all 5 selected ({len(gv)} findings)")
finally:
    fake.stop()

print()
if FAIL:
    print(f"{len(FAIL)} SCENARIO CHECK(S) FAILED:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("ALL RAG SCENARIO CHECKS PASSED (16 scenarios, all 5 languages, real PDFs)")
