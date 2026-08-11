"""RAG guideline detection against a LIVE stack (REAL LLM) — scenario suite.

    python -m tests.rag_smoke

Runs in-process on the same box as a running backend (same config/data_dir).
Ingests one real guideline PDF per language (Python, C, C++, Java, TypeScript)
through the real pypdf path, then drives the live HTTP review API — RAG toggle
on — across a scenario matrix and reports what the REAL model does: violations
detected + cited to the right PDF/rule/page, compliant code staying silent,
cross-collection attribution, retrieval faithfulness, and inline fixes. The
Phase-2 analogue of tests/accuracy_eval.py, across ALL FIVE languages.

Retrieval uses whatever rag.embedder the stack is configured with (default
'hash' — the out-of-box pilot config). Env: KOOSYS_URL (default :8000).
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("KOOSYS_URL", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RAG_SMOKE_USER", "anonymous")


# ------------------------------------------------------------ real PDF builder
def make_pdf(pages: list[str]) -> bytes:
    """Multi-page PDF with a byte-accurate xref (parses on strict pypdf)."""
    n = len(pages)
    page_nums = [3 + 2 * i for i in range(n)]
    content_nums = [4 + 2 * i for i in range(n)]
    font_obj = 3 + 2 * n
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
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


# one guideline PDF per language (the C one is two pages -> page-2 citation)
PDFS = {
    "c": ("ACME_Secure_C_Standard.pdf", make_pdf([
        "ACME Secure C Coding Standard v1.2\n"
        "Rule SEC-01: The gets function must never be used because it cannot "
        "bound its input and leads to buffer overflows. Use fgets with a size.\n"
        "Rule SEC-02: Do not use strcpy or strcat. Prefer strncpy or snprintf "
        "with an explicit size bound.",
        "Rule MEM-03: Every pointer returned by malloc must be checked against "
        "NULL before it is dereferenced.\n"
        "Rule CTRL-04: The goto statement is forbidden. Use structured control "
        "flow such as loops and helper functions instead."])),
    "py": ("ACME_Python_Guidelines.pdf", make_pdf([
        "ACME Python Engineering Guidelines\n"
        "Rule PYLOG-01: Do not use print in library modules. Use the logging "
        "module so that output can be controlled and routed.\n"
        "Rule PYSEC-02: Never call eval or exec on external or untrusted input; "
        "it enables code injection.\n"
        "Rule PYDOC-03: Every TODO comment must reference a tracking ticket, "
        "for example TODO PROJ-123."])),
    "java": ("ACME_Java_Guidelines.pdf", make_pdf([
        "ACME Java Engineering Guidelines\n"
        "Rule JAVA-01: Do not call System.out.println in production code. Route "
        "output through a logging framework such as SLF4J.\n"
        "Rule JAVA-02: Never catch the generic Exception type; catch the "
        "specific exceptions you can actually handle.\n"
        "Rule JAVA-03: Do not compare strings with the == operator; use the "
        "equals method instead."])),
    "ts": ("ACME_TypeScript_Guidelines.pdf", make_pdf([
        "ACME TypeScript Engineering Guidelines\n"
        "Rule TS-01: Do not use the any type. Declare precise types so the "
        "compiler can check them.\n"
        "Rule TS-02: Always use strict equality (===) and never the loose == "
        "operator.\n"
        "Rule TS-03: Do not leave console.log calls in committed code; use the "
        "project logger."])),
    "cpp": ("ACME_Cpp_Guidelines.pdf", make_pdf([
        "ACME C++ Engineering Guidelines\n"
        "Rule CPP-01: Do not put using namespace std at global or namespace "
        "scope in headers or shared code.\n"
        "Rule CPP-02: Use nullptr for null pointers, never NULL or the literal "
        "0.\n"
        "Rule CPP-03: Do not manage memory with raw new and delete; use smart "
        "pointers such as std::make_unique."])),
}

# violating + compliant code per language
CODE = {
    "c": (
        '#include <stdio.h>\nint main(void){\n  char buf[64];\n'
        '  gets(buf);\n  char d[8];\n  strcpy(d, buf);\n  return 0;\n}\n',
        '#include <stdio.h>\nint main(void){\n  char buf[64];\n'
        '  fgets(buf, sizeof(buf), stdin);\n  return 0;\n}\n'),
    "py": (
        'def run(cmd):\n    print("running")\n    return eval(cmd)\n'
        '# TODO: harden this later\n',
        'import logging\nlog = logging.getLogger(__name__)\n'
        'def run(cmd):\n    log.info("running")\n'
        '    # TODO(PROJ-123): support dry-run\n    return 0\n'),
    "java": (
        'public class Svc {\n  String check(String a, String b) {\n'
        '    System.out.println("checking");\n'
        '    if (a == b) { return "same"; }\n'
        '    try { return a.trim(); } catch (Exception e) { return ""; }\n'
        '  }\n}\n',
        'import org.slf4j.Logger;\nimport org.slf4j.LoggerFactory;\n'
        'public class Svc {\n'
        '  private static final Logger log = LoggerFactory.getLogger(Svc.class);\n'
        '  String check(String a, String b) {\n    log.info("checking");\n'
        '    if (a.equals(b)) { return "same"; }\n'
        '    try { return a.trim(); } catch (NullPointerException e) { return ""; }\n'
        '  }\n}\n'),
    "ts": (
        'function parse(input: any): number {\n  console.log(input);\n'
        '  if (input == null) { return 0; }\n  return input.length;\n}\n',
        'function parse(input: string | null): number {\n'
        '  if (input === null) { return 0; }\n  return input.length;\n}\n'),
    "cpp": (
        '#include <cstddef>\nusing namespace std;\n'
        'int* make() {\n  int* p = new int(5);\n'
        '  if (p == NULL) { return NULL; }\n  return p;\n}\n',
        '#include <memory>\n'
        'std::unique_ptr<int> make() {\n'
        '  auto p = std::make_unique<int>(5);\n'
        '  if (p == nullptr) { return nullptr; }\n  return p;\n}\n'),
}


def review(client, code, lang, cids):
    r = client.post(f"{BASE}/api/review", json={
        "code": code, "filename": f"m.{lang}", "language": lang,
        "rag_enabled": True, "collection_ids": cids})
    r.raise_for_status()
    jid = r.json()["job_id"]
    for _ in range(300):
        time.sleep(2)
        j = client.get(f"{BASE}/api/job/{jid}").json()
        if j["state"] in ("done", "error"):
            return j
    raise TimeoutError("review did not finish")


def gviol(job):
    return [v for v in job.get("violations", []) if v["layer"] == "guideline"]


def show(gv):
    for v in gv:
        cite = v.get("citation") or {}
        fix = v.get("fix") or {}
        tag = " +fix" if fix.get("validated") else ""
        print(f"    L{v['line']} [{v['severity']}] {v['message'][:76]}{tag}")
        print(f"       cites p{cite.get('page')} of {cite.get('source')}: "
              f"{(cite.get('quote') or '')[:64]}")


def main() -> None:
    from backend.rag import ingest, store
    health = httpx.get(f"{BASE}/api/health", timeout=10).json()
    if not health.get("rag_enabled"):
        print("rag not enabled on this stack")
        sys.exit(2)
    if not health.get("llm_available"):
        print("LLM offline — guideline layer needs the model")
        sys.exit(2)

    cid: dict[str, str] = {}
    src: dict[str, str] = {}
    for lang, (name, blob) in PDFS.items():
        c = store.create_collection(USER, f"{lang} guidelines")["id"]
        n = ingest.ingest_pdf(USER, c, name, blob)
        cid[lang], src[lang] = c, name
        print(f"ingested {name} -> {n} chunk(s)")
    print()

    client = httpx.Client(timeout=120)
    # (label, code, lang, collections, expected source PDF | None for SILENT)
    scenarios = [
        ("S1  C   gets+strcpy", CODE["c"][0], "c", [cid["c"]], src["c"]),
        ("S2  C   compliant", CODE["c"][1], "c", [cid["c"]], None),
        ("S4  Py  eval+print+TODO", CODE["py"][0], "py", [cid["py"]], src["py"]),
        ("S5  Py  compliant", CODE["py"][1], "py", [cid["py"]], None),
        ("S8  Java println+==+catch", CODE["java"][0], "java", [cid["java"]], src["java"]),
        ("S9  Java compliant", CODE["java"][1], "java", [cid["java"]], None),
        ("S10 TS  any+console+==", CODE["ts"][0], "ts", [cid["ts"]], src["ts"]),
        ("S11 TS  compliant", CODE["ts"][1], "ts", [cid["ts"]], None),
        ("S12 C++ using-std+new+NULL", CODE["cpp"][0], "cpp", [cid["cpp"]], src["cpp"]),
        ("S13 C++ compliant", CODE["cpp"][1], "cpp", [cid["cpp"]], None),
        # cross-cutting checks (attribution / faithfulness / mixed selection)
        ("S6  both C+Py, C bug", CODE["c"][0], "c", [cid["c"], cid["py"]], src["c"]),
        ("S7  C bug, only Py PDF", CODE["c"][0], "c", [cid["py"]], None),
        ("S14 all 5 selected, TS bug", CODE["ts"][0], "ts", list(cid.values()), src["ts"]),
    ]

    detected = expected_hits = fp = cite_ok = cite_tot = fixes = 0
    for label, code, lang, cids, want_src in scenarios:
        try:
            job = review(client, code, lang, cids)
        except Exception as e:  # noqa: BLE001
            print(f"== {label}: ERROR {type(e).__name__}: {e}")
            continue
        gv = gviol(job)
        verdict = "silent" if not gv else f"{len(gv)} finding(s)"
        want = "SILENT" if want_src is None else f"cite {want_src}"
        print(f"== {label}: got {verdict} | expect {want}")
        show(gv)
        if want_src is None:
            fp += len(gv)  # any finding on compliant / wrong-collection is a FP
        else:
            expected_hits += 1
            if gv:
                detected += 1
            for v in gv:
                cite_tot += 1
                if (v.get("citation") or {}).get("source") == want_src:
                    cite_ok += 1
                if (v.get("fix") or {}).get("validated"):
                    fixes += 1
        print()

    for c in cid.values():
        store.delete_collection(USER, c)

    print("=" * 60)
    print("languages covered:        Python, C, C++, Java, TypeScript")
    print(f"violations detected:      {detected}/{expected_hits} violating scenarios")
    print(f"citations to correct PDF: {cite_ok}/{cite_tot} findings")
    print(f"false positives (clean/wrong-collection): {fp}  (want 0)")
    print(f"validated inline fixes on findings: {fixes}")
    ok = (detected == expected_hits and fp == 0
          and cite_tot > 0 and cite_ok == cite_tot)
    print("\nRAG SMOKE:", "PASS" if ok else
          "REVIEW (see per-scenario lines above — real-model recall/precision)")


if __name__ == "__main__":
    main()
