#!/usr/bin/env python3
"""cnxml_mask.py - mask -> translate -> restore pipeline for OpenStax CNXML modules.

Idea: the model only ever sees prose. Formulas (MathML), images and inline markup
are replaced by tokens, then put back exactly as they were.

Token forms inside a segment (U+27E6 / U+27E7 brackets):
  ⟦M3⟧            opaque element (a formula or an inline image), restored verbatim
  ⟦2⟧ ... ⟦/2⟧    inline element with content (emphasis, term, link, ...)
  ⟦4/⟧            empty inline element (newline, space, empty link)

Subcommands:
  extract   MODULE... | --all   write skeleton.cnxml, masks.json, segments.json per module
  check     WORKDIR TRANSLATIONS.json     validate translated segments
  restore   WORKDIR TRANSLATIONS.json --out FILE.cnxml
  terms     --src DIR --collection FILE --out FILE.tsv    draft glossary from <term> tags
  selftest  --src DIR --collection FILE    extract -> restore with no translation == original
"""
import argparse, collections, copy, json, os, re, sys
from lxml import etree

NS = "http://cnx.rice.edu/cnxml"
NS_MD = "http://cnx.rice.edu/mdml"
NS_M = "http://www.w3.org/1998/Math/MathML"
OPAQUE = {f"{{{NS}}}media", f"{{{NS_M}}}math"}
INLINE = {f"{{{NS}}}{n}" for n in
          ("emphasis", "term", "link", "sup", "sub", "span", "foreign", "code",
           "quote", "cite", "newline", "space")}
TOKEN = re.compile(r"⟦(/?)(M?\d+)(/?)⟧")
WORD = re.compile(r"[A-Za-z]{2,}")
PI_TARGET = "seg"


# ---------------------------------------------------------------- helpers
def local(el):
    return etree.QName(el).localname


def inline_ok(el):
    if not isinstance(el.tag, str):
        return False
    if el.tag in OPAQUE:
        return True
    if el.tag in INLINE:
        return all(inline_ok(c) for c in el)
    return False


def skipped(el):
    return (isinstance(el.tag, str) and el.tag.startswith("{" + NS_MD + "}")
            and local(el) not in ("title", "abstract"))


def ser(el):
    return etree.tostring(el, encoding="unicode", with_tail=False)


def module_ids(collection):
    s = open(collection, encoding="utf8").read()
    return re.findall(r'<col:module document="(m\d+)"', s)


# ---------------------------------------------------------------- encode
def encode(items, masks, counter):
    out = []
    for it in items:
        if isinstance(it, str):
            assert "⟦" not in it and "⟧" not in it, "token bracket in source text"
            out.append(it)
            continue
        counter[0] += 1
        n = counter[0]
        if it.tag in OPAQUE:
            masks["tokens"][f"M{n}"] = {"xml": ser(it)}
            out.append(f"⟦M{n}⟧")
        elif len(it) == 0 and not (it.text or "").strip():
            masks["tokens"][str(n)] = {"tag": it.tag, "attrib": dict(it.attrib)}
            out.append(f"⟦{n}/⟧")
        else:
            masks["tokens"][str(n)] = {"tag": it.tag, "attrib": dict(it.attrib)}
            inner = [it.text] if it.text else []
            for c in it:
                inner.append(c)
                if c.tail:
                    inner.append(c.tail)
            out.append(f"⟦{n}⟧")
            out.extend(encode(inner, masks, counter))
            out.append(f"⟦/{n}⟧")
    return out


class Extractor:
    def __init__(self):
        self.segments = {}
        self.masks = {}
        self.k = 0

    def run(self, E):
        # split E's content into runs of inline content and block children
        nodes, run = [], []
        if E.text:
            run.append(E.text)
        for c in list(E):
            if inline_ok(c):
                run.append(c)
                if c.tail:
                    run.append(c.tail)
            else:
                if run:
                    nodes.append(("run", run)); run = []
                nodes.append(("blk", c))
                if c.tail:
                    run.append(c.tail)
        if run:
            nodes.append(("run", run))
        for kind, x in nodes:
            if kind == "blk" and isinstance(x.tag, str) and not skipped(x):
                self.run(x)
        # rebuild E
        E.text = None
        for c in list(E):
            E.remove(c)

        def emit_str(s):
            if len(E) == 0:
                E.text = (E.text or "") + s
            else:
                E[-1].tail = (E[-1].tail or "") + s

        for kind, x in nodes:
            if kind == "blk":
                x.tail = None
                E.append(x)
                continue
            masks = {"tokens": {}}
            text = "".join(encode(x, masks, [0]))
            core = text.strip()
            if not WORD.search(TOKEN.sub(" ", core)):
                for it in x:                      # nothing to translate: put back verbatim
                    emit_str(it) if isinstance(it, str) else (setattr(it, "tail", None), E.append(it))
                continue
            self.k += 1
            sid = f"S{self.k:04d}"
            masks["lead"] = text[:len(text) - len(text.lstrip())]
            masks["trail"] = text[len(text.rstrip()):]
            self.segments[sid] = core
            self.masks[sid] = masks
            E.append(etree.ProcessingInstruction(PI_TARGET, sid))


def extract_module(path):
    tree = etree.parse(path)
    ex = Extractor()
    ex.run(tree.getroot())
    return tree, ex.segments, ex.masks


# ---------------------------------------------------------------- decode
def parse_tokens(text):
    """-> list of ('t', str) | ('open'|'close'|'empty'|'opaque', id)"""
    out, pos = [], 0
    for m in TOKEN.finditer(text):
        if m.start() > pos:
            out.append(("t", text[pos:m.start()]))
        close, tid, slash = m.groups()
        if tid.startswith("M"):
            out.append(("opaque", tid))
        elif close:
            out.append(("close", tid))
        elif slash:
            out.append(("empty", tid))
        else:
            out.append(("open", tid))
        pos = m.end()
    if pos < len(text):
        out.append(("t", text[pos:]))
    return out


def token_problems(orig, trans):
    po, pt = parse_tokens(orig), parse_tokens(trans)
    co = collections.Counter((k, v) for k, v in po if k != "t")
    ct = collections.Counter((k, v) for k, v in pt if k != "t")
    probs = []
    for key in co - ct:
        probs.append(f"missing {key[0]} {key[1]}")
    for key in ct - co:
        probs.append(f"extra {key[0]} {key[1]}")
    stack = []
    for k, v in pt:
        if k == "open":
            stack.append(v)
        elif k == "close":
            if not stack or stack.pop() != v:
                probs.append(f"bad nesting at /{v}")
                break
    if stack:
        probs.append("unclosed " + ",".join(stack))
    return probs


def content_problems(orig, trans):
    probs = []
    bare_o = TOKEN.sub(" ", orig)
    bare_t = TOKEN.sub(" ", trans)
    if len(WORD.findall(bare_o)) >= 3 and not re.search(r"[\u4e00-\u9fff]", bare_t):
        probs.append("looks untranslated (no CJK)")
    for bad in ("$", "\\(", "\\[", "```", "**", "<m:", "<math"):
        if bad in trans:
            probs.append(f"leaked markup {bad!r}")
    return probs


def build_nodes(text, masks, container):
    """masked text -> (lead_text, [elements]) built as children of container"""
    toks = parse_tokens(text)
    stack, first = [container], None
    lead = ""

    def add_text(s):
        nonlocal lead
        parent = stack[-1]
        if len(parent) == 0:
            if parent is container:
                lead += s
            else:
                parent.text = (parent.text or "") + s
        else:
            parent[-1].tail = (parent[-1].tail or "") + s

    for k, v in toks:
        if k == "t":
            add_text(v)
        elif k == "opaque":
            el = etree.fromstring(masks["tokens"][v]["xml"])
            stack[-1].append(el)
        else:
            info = masks["tokens"][v]
            if k == "close":
                stack.pop()
                continue
            el = etree.SubElement(stack[-1], info["tag"], info["attrib"])
            if k == "open":
                stack.append(el)
    return lead


def restore_tree(skeleton_path, masks, translations, strict_fallback=True):
    tree = etree.parse(skeleton_path)
    root = tree.getroot()
    report = {"translated": 0, "fallback": []}
    for pi in list(root.iter(etree.ProcessingInstruction)):
        if pi.target != PI_TARGET:
            continue
        sid = pi.text.strip()
        m = masks[sid]
        orig = m["_orig"]
        trans = translations.get(sid)
        invalid = (trans is None or token_problems(orig, trans)
                   or (trans != orig and content_problems(orig, trans)))
        if invalid or trans == orig:
            report["fallback"].append(sid)
            trans = orig
        else:
            report["translated"] += 1
        text = m["lead"] + trans.strip() + m["trail"]
        container = etree.Element(f"{{{NS}}}tmp", nsmap=root.nsmap)
        lead = build_nodes(text, m, container)
        parent = pi.getparent()
        idx = parent.index(pi)
        tail = pi.tail
        if lead:
            if idx == 0:
                parent.text = (parent.text or "") + lead
            else:
                parent[idx - 1].tail = (parent[idx - 1].tail or "") + lead
        for off, el in enumerate(list(container)):
            parent.insert(idx + off, el)
        parent.remove(pi)
        if tail:
            if len(container):
                last = list(container)[-1]
                last.tail = (last.tail or "") + tail
            elif idx == 0:
                parent.text = (parent.text or "") + tail
            else:
                parent[idx - 1].tail = (parent[idx - 1].tail or "") + tail
    etree.cleanup_namespaces(root)
    return tree, report


# ---------------------------------------------------------------- commands
def write_work(mid, src, out):
    tree, segs, masks = extract_module(os.path.join(src, mid, "index.cnxml"))
    d = os.path.join(out, mid)
    os.makedirs(d, exist_ok=True)
    tree.write(os.path.join(d, "skeleton.cnxml"), encoding="utf-8")
    for sid in masks:
        masks[sid]["_orig"] = segs[sid]
    json.dump(segs, open(os.path.join(d, "segments.json"), "w", encoding="utf8"), ensure_ascii=False, indent=1)
    json.dump(masks, open(os.path.join(d, "masks.json"), "w", encoding="utf8"), ensure_ascii=False)
    return segs


def load_work(d):
    segs = json.load(open(os.path.join(d, "segments.json"), encoding="utf8"))
    masks = json.load(open(os.path.join(d, "masks.json"), encoding="utf8"))
    return segs, masks


def cmd_extract(a):
    ids = module_ids(a.collection) if a.all else a.modules
    tot_w = 0
    for mid in ids:
        segs = write_work(mid, a.src, a.out)
        w = sum(len(TOKEN.sub(" ", s).split()) for s in segs.values())
        tot_w += w
        print(f"{mid}: {len(segs)} segments, {w} words")
    print(f"total words to translate: {tot_w}")


def cmd_check(a):
    segs, masks = load_work(a.workdir)
    trans = json.load(open(a.translations, encoding="utf8"))
    bad, ok, missing = [], 0, 0
    for sid, orig in segs.items():
        t = trans.get(sid)
        if t is None:
            missing += 1
            continue
        p = token_problems(orig, t) + content_problems(orig, t)
        if p:
            bad.append((sid, p))
        else:
            ok += 1
    extra = set(trans) - set(segs)
    print(f"segments {len(segs)} | ok {ok} | problems {len(bad)} | missing {missing} | unknown ids {len(extra)}")
    for sid, p in bad[:50]:
        print(f"  {sid}: {'; '.join(p)}")
    sys.exit(1 if bad or extra else 0)


def cmd_restore(a):
    segs, masks = load_work(a.workdir)
    trans = json.load(open(a.translations, encoding="utf8"))
    tree, rep = restore_tree(os.path.join(a.workdir, "skeleton.cnxml"), masks, trans)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    tree.write(a.out, encoding="utf-8")
    print(f"restored: translated {rep['translated']}, kept English {len(rep['fallback'])} -> {a.out}")
    if rep["fallback"]:
        print("  English kept for:", ", ".join(rep["fallback"][:30]))
    if a.strict and rep["fallback"]:
        sys.exit(1)


def cmd_terms(a):
    cnt, first, defs = collections.Counter(), {}, {}
    for mid in module_ids(a.collection):
        t = etree.parse(os.path.join(a.src, mid, "index.cnxml"))
        for e in t.iter(f"{{{NS}}}term"):
            w = " ".join("".join(e.itertext()).split()).lower()
            if w:
                cnt[w] += 1
                first.setdefault(w, mid)
        for d in t.iter(f"{{{NS}}}definition"):
            te, me = d.find(f"{{{NS}}}term"), d.find(f"{{{NS}}}meaning")
            if te is not None and me is not None:
                k = " ".join("".join(te.itertext()).split()).lower()
                defs[k] = " ".join("".join(me.itertext()).split())[:120]
    with open(a.out, "w", encoding="utf8") as f:
        f.write("english\tcount\tfirst_module\tchinese(fill in)\tenglish_meaning\n")
        for w, n in cnt.most_common():
            f.write(f"{w}\t{n}\t{first[w]}\t\t{defs.get(w, '')}\n")
    print(f"{len(cnt)} distinct terms -> {a.out}")


def cmd_selftest(a):
    bad, nseg = 0, 0
    for mid in module_ids(a.collection):
        p = os.path.join(a.src, mid, "index.cnxml")
        orig = etree.parse(p)
        tree, segs, masks = extract_module(p)
        for sid in masks:
            masks[sid]["_orig"] = segs[sid]
        tmp = f"/tmp/_skel_{mid}.cnxml"
        tree.write(tmp, encoding="utf-8")
        res, rep = restore_tree(tmp, masks, dict(segs))
        a1 = etree.tostring(orig, method="c14n")
        a2 = etree.tostring(res, method="c14n")
        nseg += len(segs)
        if a1 != a2:
            bad += 1
            print(f"MISMATCH {mid}")
    print(f"selftest: {bad} mismatching modules, {nseg} segments")
    sys.exit(1 if bad else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    e = sp.add_parser("extract"); e.add_argument("modules", nargs="*")
    e.add_argument("--src", required=True); e.add_argument("--out", default="work")
    e.add_argument("--all", action="store_true"); e.add_argument("--collection"); e.set_defaults(f=cmd_extract)
    c = sp.add_parser("check"); c.add_argument("workdir"); c.add_argument("translations"); c.set_defaults(f=cmd_check)
    r = sp.add_parser("restore"); r.add_argument("workdir"); r.add_argument("translations")
    r.add_argument("--out", required=True); r.add_argument("--strict", action="store_true"); r.set_defaults(f=cmd_restore)
    t = sp.add_parser("terms"); t.add_argument("--src", required=True); t.add_argument("--collection", required=True)
    t.add_argument("--out", default="glossary_draft.tsv"); t.set_defaults(f=cmd_terms)
    s = sp.add_parser("selftest"); s.add_argument("--src", required=True); s.add_argument("--collection", required=True)
    s.set_defaults(f=cmd_selftest)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
