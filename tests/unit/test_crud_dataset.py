import asyncio
import copy
import json

import httpx
import pytest

from scripts import prepare_crud_rag as crud


def upstream():
    data = {task: [] for task in crud.TASKS}
    for task, n in crud.TASKS.items():
        for i in range(100):
            data[task].append(
                {
                    "ID": f"event-{i}",
                    "event": f"事件{i}",
                    "questions": f"事件{i}的{n}文档问题是什么？",
                    "answers": f"参考答案-{i}-{n}",
                    **{
                        f"news{j}": f"事件{i}新闻{j}。这是原始报道的正文。" for j in range(1, n + 1)
                    },
                }
            )
    data["event_summary"] = [
        {"ID": f"extra-{i}", "text": f"额外事件{i}的干扰候选报道。"} for i in range(30)
    ]
    return data


def bundle(tmp_path):
    folder = tmp_path / "subset"
    crud.write_subset(crud.build_subset(upstream(), per_task=4, distractors=5), folder)
    return folder


def test_group_split_deterministic_and_answers_not_indexed(tmp_path):
    data = upstream()
    first = crud.build_subset(data, distractors=5)
    for rows in data.values():
        rows.reverse()
    assert first == crud.build_subset(data, distractors=5)
    assert len(first["cases"]) == 60
    groups = {}
    for case in first["cases"]:
        assert groups.setdefault(case["group_id"], case["split"]) == case["split"]
        assert case["judgment_scope"] == "partial"
        assert case["evidence_groups"] == []
    for document in first["documents"]:
        assert "参考答案" not in document["text"]
    folder = tmp_path / "complete"
    crud.write_subset(first, folder)
    crud.validate_bundle(folder)


def test_identical_news_connect_different_events():
    data = upstream()
    for task in crud.TASKS:
        data[task][0]["news1"] = data[task][1]["news1"]
    result = crud.build_subset(data, per_task=40, distractors=0)
    assert (
        len({c["group_id"] for c in result["cases"] if c["upstream_id"] in ("event-0", "event-1")})
        <= 1
    )


def test_modified_document_is_rejected(tmp_path):
    folder = bundle(tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    (folder / manifest["documents"][0]["path"]).write_text("modified")
    with pytest.raises(ValueError, match="哈希"):
        crud.validate_bundle(folder)


def test_pinned_upstream_rejects_unknown_data(tmp_path):
    path = tmp_path / "upstream.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="上游文件校验失败"):
        crud.read_upstream(path)


def test_cross_split_group_rejected(tmp_path):
    folder = bundle(tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    a = manifest["cases"][0]
    b = next(c for c in manifest["cases"] if c["split"] != a["split"])
    b["group_id"] = a["group_id"]
    crud.save_json(folder / "manifest.json", manifest)
    with pytest.raises(ValueError, match="跨集合"):
        crud.validate_bundle(folder)


def test_import_uploads_news_only_and_preserves_draft(tmp_path, monkeypatch):
    folder = bundle(tmp_path)
    client_class = httpx.AsyncClient
    posts = []

    def handle(request):
        if request.method == "GET":
            assert request.url.path == "/api/v1/evaluations/corpus"
            return httpx.Response(200, json=[])
        posts.append(request)
        if request.url.path.endswith("/files"):
            assert b"reference_answer" not in request.content
            name = request.headers["Idempotency-Key"]
            return httpx.Response(201, json={"file_id": name, "content_hash": name[5:]})
        payload = json.loads(request.content)
        assert payload["review_status"] == "draft" and not payload["frozen"]
        assert all(c["judgment_scope"] == "partial" for c in payload["cases"])
        assert all(not c["evidence_groups"] for c in payload["cases"])
        return httpx.Response(201, json={"case_set_id": "saved", **payload})

    monkeypatch.setattr(
        crud.httpx,
        "AsyncClient",
        lambda **kw: client_class(**kw, transport=httpx.MockTransport(handle)),
    )
    result = asyncio.run(crud.import_bundle(folder, "http://test", "crud-only"))
    assert result["case_sets"] == 2
    assert result["index_requested"] is False
    assert not any(r.url.path.endswith("/index") for r in posts)


def test_foreign_corpus_rejected_before_upload(tmp_path, monkeypatch):
    folder = bundle(tmp_path)
    client_class = httpx.AsyncClient

    def handle(request):
        assert request.method == "GET"
        return httpx.Response(200, json=[{"name": "business.txt", "content_hash": "other"}])

    monkeypatch.setattr(
        crud.httpx,
        "AsyncClient",
        lambda **kw: client_class(**kw, transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(ValueError, match="其他资料"):
        asyncio.run(crud.import_bundle(folder, "http://test", "business"))


def test_invalid_case_rejected_before_import(tmp_path):
    folder = bundle(tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    broken = copy.deepcopy(manifest)
    broken["cases"][0]["reference_answer"] = "x" * 2001
    crud.save_json(folder / "manifest.json", broken)
    with pytest.raises(ValueError):
        crud.validate_bundle(folder)


def test_review_exclusion_creates_version_without_changing_upstream(tmp_path):
    folder = bundle(tmp_path)
    original = (folder / "manifest.json").read_bytes()
    case_id = json.loads(original)["cases"][0]["case_id"]
    reviews = tmp_path / "reviews.json"
    crud.save_json(
        reviews,
        {
            "schema_version": 1,
            "reviews": [{"case_id": case_id, "action": "exclude", "reason": "incorrect reference"}],
        },
    )
    output = tmp_path / "reviewed"
    assert crud.revise_bundle(folder, output, reviews) == 1
    revised, contents = crud.validate_bundle(output)
    assert (folder / "manifest.json").read_bytes() == original
    assert all(c["case_id"] != case_id for c in revised["cases"])
    assert revised["excluded_cases"][0]["case"]["case_id"] == case_id
    assert len(contents) == len(json.loads(original)["documents"])
    assert all(c["judgment_scope"] == "partial" for c in revised["cases"])
