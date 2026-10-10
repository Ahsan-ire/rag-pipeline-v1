"""Phase 16A-1 item 5 / acceptance (k2), module parts: frozen expansion artifact.

Synthetic rows and a fake expand function only: no network, no eval files.
"""

import hashlib
import json

import pytest

import src.expansion_artifact as ea
from src.eval_privacy import SealedInputError
from src.eval_sets import classify
from src.expansion_artifact import (
    ExpansionArtifactError,
    artifact_digest,
    build_artifact,
    live_digest,
    load_artifact,
    rewrite_identity,
    save_artifact,
)
from src.query_rewrite import (
    REWRITE_MODEL,
    STATUS_API_ERROR,
    STATUS_LIVE,
    STATUS_PARSE_ERROR,
    Expansion,
)

ROWS = [
    ("syn-001", "synthetic widget question alpha"),
    ("syn-002", "synthetic gadget question beta"),
    ("syn-003", "synthetic widget question alpha"),  # same text as syn-001
    ("syn-004", "synthetic sprocket question gamma"),
]
SHA = "a" * 64


class FakeExpand:
    """Deterministic fake expand_fn that counts calls."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, question):
        self.calls.append(question)
        if question in self.fail:
            return Expansion(question, (), REWRITE_MODEL, STATUS_API_ERROR)
        tag = hashlib.sha256(question.encode()).hexdigest()[:6]
        return Expansion(
            question, (f"rw1 {tag}", f"rw2 {tag}"), REWRITE_MODEL, STATUS_LIVE, f"intent {tag}"
        )


def _inputs(sha=SHA):
    return [{"path": "tests/fixtures/p16w_synthetic.jsonl", "sha256": sha, "kind": "questions"}]


def _built(tmp_path, fake=None, rows=ROWS):
    fake = fake or FakeExpand()
    art = build_artifact(rows, fake, _inputs())
    path = tmp_path / "artifact.json"
    save_artifact(path, art)
    return path, art, fake


# --- build -------------------------------------------------------------------
def test_build_shape_and_no_question_text(tmp_path):
    path, art, fake = _built(tmp_path)
    assert set(art) == {"version", "kind", "inputs", "identity", "build", "entries"}
    assert art["identity"] == rewrite_identity()
    assert set(art["entries"]) == {r for r, _ in ROWS}
    entry = art["entries"]["syn-002"]
    assert set(entry) == {"question_sha256", "rewrites", "intent", "status"}
    assert entry["question_sha256"] == hashlib.sha256(ROWS[1][1].encode()).hexdigest()
    raw = path.read_text(encoding="utf-8")
    for _, q in ROWS:
        assert q not in raw


def test_build_record_counts_and_dedupe():
    fake = FakeExpand(fail={ROWS[3][1]})
    art = build_artifact(ROWS, fake, _inputs())
    # syn-001 and syn-003 share text: one call, two entries.
    assert len(fake.calls) == 3
    assert art["build"] == {"entries": 4, "live_attempts": 3, "fallbacks": 1}
    assert art["entries"]["syn-001"] == art["entries"]["syn-003"]


def test_live_status_with_zero_rewrites_is_a_fallback():
    def fn(q):
        return Expansion(q, (), REWRITE_MODEL, STATUS_LIVE)

    art = build_artifact(ROWS[:2], fn, _inputs())
    assert art["build"]["fallbacks"] == 2


def test_build_refuses_duplicate_row_id_bad_model_and_bad_inputs():
    with pytest.raises(ExpansionArtifactError, match="duplicate row id"):
        build_artifact([("a1", "x"), ("a1", "y")], FakeExpand(), _inputs())
    with pytest.raises(ExpansionArtifactError, match="model"):
        build_artifact([("a1", "x")], lambda q: Expansion(q, ("r",), "other-model", STATUS_LIVE), _inputs())
    with pytest.raises(ExpansionArtifactError, match="inputs"):
        build_artifact([("a1", "x")], FakeExpand(), [{"path": "p", "sha256": SHA, "kind": "derived"}])
    with pytest.raises(ExpansionArtifactError, match="inputs"):
        build_artifact([("a1", "x")], FakeExpand(), [])


# --- save / digest ---------------------------------------------------------
def test_save_is_deterministic_and_digest_is_file_sha(tmp_path):
    path, art, _ = _built(tmp_path)
    other = tmp_path / "again.json"
    digest = save_artifact(other, json.loads(path.read_text(encoding="utf-8")))
    assert path.read_bytes() == other.read_bytes()
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact_digest(path) == file_sha == digest
    assert load_artifact(path).digest == file_sha
    assert not list(tmp_path.glob("*.tmp"))


def test_save_refuses_malformed(tmp_path):
    art = build_artifact(ROWS, FakeExpand(), _inputs())
    art["build"]["entries"] = 99
    with pytest.raises(ExpansionArtifactError):
        save_artifact(tmp_path / "bad.json", art)
    assert not (tmp_path / "bad.json").exists()


# --- replay ----------------------------------------------------------------
def test_replay_identical_for_every_row(tmp_path):
    path, _, fake = _built(tmp_path)
    live = {row_id: FakeExpand()(q) for row_id, q in ROWS}
    a1, a2 = load_artifact(path), load_artifact(path)
    for row_id, q in ROWS:
        r1, r2 = a1.replay(row_id, q), a2.replay(row_id, q)
        assert r1 == r2 == live[row_id]
    assert len(fake.calls) == 3  # only the build called expand


def test_missing_entry_fails_before_any_expand_call(tmp_path):
    path, _, _ = _built(tmp_path, rows=ROWS[:2])
    art = load_artifact(path)
    fake = FakeExpand()
    with pytest.raises(ExpansionArtifactError, match="missing entry for row syn-004"):
        art.preflight(ROWS)
    assert fake.calls == []
    with pytest.raises(ExpansionArtifactError, match="missing entry"):
        art.replay("syn-004", ROWS[3][1])


def test_question_sha_mismatch_refuses_without_echoing_text(tmp_path):
    path, _, _ = _built(tmp_path)
    art = load_artifact(path)
    changed = "synthetic gadget question CHANGED"
    with pytest.raises(ExpansionArtifactError, match="question sha256 mismatch for row syn-002") as exc:
        art.preflight([("syn-002", changed)])
    assert changed not in str(exc.value)
    with pytest.raises(ExpansionArtifactError, match="mismatch"):
        art.replay("syn-002", changed)


@pytest.mark.parametrize("field", ["model", "prompt_sha256", "config_hash"])
def test_identity_mismatch_refuses(tmp_path, field):
    path, _, _ = _built(tmp_path)
    art = load_artifact(path)
    art.check_identity()  # matches the current code
    art.preflight(ROWS)
    other = dict(rewrite_identity())
    other[field] = "claude-other" if field == "model" else "b" * 64
    with pytest.raises(ExpansionArtifactError, match=f"identity mismatch: {field}"):
        art.check_identity(other)
    with pytest.raises(ExpansionArtifactError, match=field):
        art.preflight(ROWS, identity=other)


def test_identity_tracks_prompt_and_config(monkeypatch):
    base = rewrite_identity()
    monkeypatch.setattr(ea.qr, "REWRITE_MAX_TOKENS", 301)
    bumped = rewrite_identity()
    assert bumped["prompt_sha256"] == base["prompt_sha256"]
    assert bumped["config_hash"] != base["config_hash"]
    monkeypatch.undo()
    monkeypatch.setattr(ea, "_prompt_messages", lambda: [["system", "other prompt"]])
    changed = rewrite_identity()
    assert changed["prompt_sha256"] != base["prompt_sha256"]
    assert changed["config_hash"] != base["config_hash"]


# --- load: sealed refusal and schema ----------------------------------------
def test_load_refuses_sealed_before_parsing(tmp_path, _private_root_in_tmp):
    sealed_dir = _private_root_in_tmp / "sealed"
    sealed_dir.mkdir(parents=True)
    p = sealed_dir / "art.json"
    p.write_text("not json at all", encoding="utf-8")
    with pytest.raises(SealedInputError):
        load_artifact(p)
    marked = tmp_path / "marked.json"
    art = build_artifact(ROWS, FakeExpand(), _inputs())
    art["sealed"] = True
    marked.write_text(json.dumps(art), encoding="utf-8")
    with pytest.raises(SealedInputError):
        load_artifact(marked)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.pop("inputs"),
        lambda a: a.__setitem__("version", 2),
        lambda a: a["entries"]["syn-001"].__setitem__("status", "bogus"),
        lambda a: a["entries"]["syn-001"].__setitem__("question_sha256", "xyz"),
        lambda a: a["entries"]["syn-001"].__setitem__("question", "text"),
        lambda a: a["identity"].pop("config_hash"),
        lambda a: a["build"].__setitem__("fallbacks", 9),
    ],
)
def test_load_schema_validation(tmp_path, mutate):
    art = build_artifact(ROWS, FakeExpand(), _inputs())
    mutate(art)
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    with pytest.raises(ExpansionArtifactError):
        load_artifact(p)


def test_load_non_json_and_missing(tmp_path):
    p = tmp_path / "junk.json"
    p.write_text("{", encoding="utf-8")
    with pytest.raises(ExpansionArtifactError):
        load_artifact(p)
    with pytest.raises(ExpansionArtifactError):
        load_artifact(tmp_path / "absent.json")


def test_artifact_from_public_inputs_classifies_public(tmp_path, eval_registry):
    qset = tmp_path / "public_set.jsonl"
    qset.write_text(json.dumps({"question": "synthetic q"}) + "\n", encoding="utf-8")
    entry = eval_registry.add(qset)
    art = build_artifact([("syn-001", "synthetic q")], FakeExpand(), _inputs(entry.sha256))
    p = tmp_path / "pub_art.json"
    save_artifact(p, art)
    assert classify(p) == "public"
    assert load_artifact(p).privacy == "public"
    p2 = tmp_path / "priv_art.json"
    save_artifact(p2, build_artifact([("syn-001", "synthetic q")], FakeExpand(), _inputs("c" * 64)))
    assert classify(p2) == "private"


# --- live digest ---------------------------------------------------------------
def _live_entries():
    fake = FakeExpand()
    return {row_id: fake(q) for row_id, q in ROWS}


def test_live_digest_order_independent():
    entries = _live_entries()
    reversed_entries = dict(reversed(list(entries.items())))
    assert live_digest(entries) == live_digest(reversed_entries)
    assert len(live_digest(entries)) == 64


def test_live_digest_changes_with_any_rewrite_or_intent():
    entries = _live_entries()
    base = live_digest(entries)
    e = entries["syn-002"]
    changed_rw = dict(entries, **{"syn-002": Expansion(e.original, (e.rewrites[0], "different"), e.model, e.status, e.intent_rewrite)})
    changed_intent = dict(entries, **{"syn-002": Expansion(e.original, e.rewrites, e.model, e.status, "other intent")})
    no_intent = dict(entries, **{"syn-002": Expansion(e.original, e.rewrites, e.model, e.status, None)})
    digests = {base, live_digest(changed_rw), live_digest(changed_intent), live_digest(no_intent)}
    assert len(digests) == 4


def test_live_digest_ignores_status_and_original():
    entries = _live_entries()
    e = entries["syn-001"]
    alt = dict(entries, **{"syn-001": Expansion("other", e.rewrites, e.model, STATUS_PARSE_ERROR, e.intent_rewrite)})
    assert live_digest(alt) == live_digest(entries)
