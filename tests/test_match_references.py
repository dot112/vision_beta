"""The pattern check's files: a reference set is written once into data/match-references/<id>/ and read
back the same; a draft (the training picture, its boxes and mask, the good samples) lives per camera in
data/match-references/drafts/<camera>/ and survives a restart; the clean-up keeps every set a line's
camera, a saved line setup or a draft names, and sets made in the last 24 hours.
"""
from __future__ import annotations

import json
import os
import time
from collections import namedtuple

import cv2
import numpy as np
import pytest

from app.engines.pattern_training import DraftData, train
from app.services import match_references
from app.services.match_references import (ReferenceMissing, add_sample, clean_up, delete_draft, draft, draft_data,
                                           draft_from_set, load_set, new_id, remove_sample, room_ok, save_boxes,
                                           save_mask, set_info, set_trained, start_draft, write_set)
from tests.pattern_pictures import good_pictures, label_box

PICTURES = good_pictures()[:3]
LABEL_BOX, CAP_BOX = (-180, -120, 180, 120), (84, 34, 156, 106)


@pytest.fixture(scope="module")
def trained():
    ignore = np.zeros((960, 1280), bool)
    ignore[553:585, 485:660] = True
    made = DraftData(PICTURES[0], label_box(LABEL_BOX), [label_box(CAP_BOX)], ignore, PICTURES)
    ref, _ = train(made, 15, [{"colour": False, "sensitivity": "normal"}] * 2, "ref-0a1b2c3d")
    return ref, made


@pytest.fixture(autouse=True)
def _forget():
    match_references._cache.clear()
    yield
    match_references._cache.clear()


def _png(mask):
    return cv2.imencode(".png", mask.astype(np.uint8) * 255)[1].tobytes()


def test_new_ids():
    ids = {new_id() for _ in range(20)}
    assert len(ids) == 20 and all(match_references.ID_RE.fullmatch(i) for i in ids)


def test_a_set_is_written_once_and_loaded(data_dir, trained):
    ref, made = trained
    write_set(ref, made, "admin", "c1")
    folder = data_dir / "match-references" / ref.id
    assert (folder / "set.json").is_file() and (folder / "mask.png").is_file() and len(list((folder / "samples").iterdir())) == 3
    assert not list(data_dir.glob("**/*.partial"))
    meta = json.loads((folder / "set.json").read_text(encoding="utf-8"))
    assert meta["made_by"] == "admin" and meta["camera_id"] == "c1" and meta["picture_size"] == [1280, 960]
    back = load_set(ref.id)
    assert back.picture_size == (1280, 960) and back.samples == 3 and len(back.checks) == 2
    for mine, theirs in zip(ref.checks, back.checks):
        assert tuple(theirs.box) == tuple(mine.box) and abs(theirs.typical - mine.typical) < 1e-6
        assert np.array_equal(theirs.mean, mine.mean) and np.allclose(theirs.spread, mine.spread, atol=0.05)
        assert np.array_equal(theirs.ignore, mine.ignore)
    assert back.checks[0].ignore.any() and load_set(ref.id) is back          # kept in memory
    with pytest.raises(FileExistsError):
        write_set(ref, made, "admin", "c1")


def test_a_missing_or_broken_set(data_dir, trained):
    with pytest.raises(ReferenceMissing, match="ref-00000000 is missing"):
        load_set("ref-00000000")
    with pytest.raises(ReferenceMissing):
        load_set("../escape")
    ref, made = trained
    write_set(ref, made, "admin", "c1")
    (data_dir / "match-references" / ref.id / "set.json").write_text('{"format": 1, "id"', encoding="utf-8")
    with pytest.raises(ReferenceMissing, match="cannot be read"):
        load_set(ref.id)
    assert set_info(ref.id) is None and set_info("ref-11111111") is None


def test_set_info(data_dir, trained):
    ref, made = trained
    write_set(ref, made, "admin", "c1")
    assert set_info(ref.id) == {"checks": 2, "reference_size": [360, 240], "picture_size": [1280, 960]}


def test_a_draft_survives_and_takes_boxes_mask_and_samples(data_dir):
    assert draft("c1") is None and draft_data("c1") is None
    start_draft("c1", PICTURES[0])
    save_boxes("c1", [0.359375, 0.375, 0.640625, 0.625], [[0.565625, 0.535417, 0.621875, 0.610417]], [0.1, 0.1, 0.9, 0.9])
    save_mask("c1", _png(np.pad(np.ones((10, 10), bool), ((0, 950), (0, 1270)))))
    with pytest.raises(ValueError, match="1280 × 960"):
        save_mask("c1", _png(np.zeros((100, 100), bool)))
    assert add_sample("c1", PICTURES[1], 98) == 2 and add_sample("c1", PICTURES[2], 99) == 3
    remove_sample("c1", 2)
    with pytest.raises(ValueError, match="sample 1"):
        remove_sample("c1", 1)
    match_references._cache.clear()                                        # as after a restart: all from disk
    shown = draft("c1")
    assert shown["reference"] == [0.3594, 0.375, 0.6406, 0.625] and shown["search_area"] == [0.1, 0.1, 0.9, 0.9]
    assert shown["samples"] == [{"n": 1, "score": 100}, {"n": 2, "score": 99}] and shown["picture_size"] == [1280, 960]
    assert shown["trained"] is None and len(shown["checks"]) == 1
    data = draft_data("c1")
    assert data.reference == [460, 360, 820, 600] and data.checks == [[724, 514, 796, 586]]
    assert data.ignore[:10, :10].all() and data.ignore.sum() == 100 and len(data.samples) == 2
    assert np.array_equal(data.picture, PICTURES[0]) and data.samples[0] is data.picture
    set_trained("c1", "ref-0a1b2c3d")
    assert draft("c1")["trained"] == "ref-0a1b2c3d"
    delete_draft("c1")
    assert draft("c1") is None and not (data_dir / "match-references" / "drafts" / "c1").exists()


def test_a_draft_from_a_set(data_dir, trained):
    ref, made = trained
    write_set(ref, made, "admin", "c1")
    draft_from_set("c2", ref.id)
    data = draft_data("c2")
    assert data.reference == made.reference and data.checks == made.checks and np.array_equal(data.ignore, made.ignore)
    assert len(data.samples) == 3 and draft("c2")["trained"] == ref.id
    with pytest.raises(ReferenceMissing):
        draft_from_set("c2", "ref-99999999")


def test_a_camera_id_cannot_leave_the_folder(data_dir):
    with pytest.raises(KeyError):
        start_draft("../x", PICTURES[0])


def _age(path, hours):
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_clean_up_keeps_what_is_named(data_dir, trained):
    ref, made = trained
    ids = ["ref-00000001", "ref-00000002", "ref-00000003", "ref-00000004", "ref-00000005"]
    for set_id in ids:
        write_set(type(ref)(set_id, ref.picture_size, ref.checks, ref.samples), made, "admin", "c1")
        _age(data_dir / "match-references" / set_id / "set.json", 25)
    _age(data_dir / "match-references" / ids[4] / "set.json", 1)          # made an hour ago
    saved = data_dir / "line-setups" / "line-1"
    saved.mkdir(parents=True)
    (saved / "setup-0000aaaa.json").write_text(json.dumps(
        {"line": {"cameras": [{"camera_id": "m1", "role": "match", "match": {"reference_id": ids[1]}}]}}), encoding="utf-8")
    (saved / "setup-broken.json").write_text("{", encoding="utf-8")
    start_draft("m2", PICTURES[0])
    set_trained("m2", ids[2])
    start_draft("m3", PICTURES[0])
    _age(data_dir / "match-references" / "drafts" / "m3" / "draft.json", 31 * 24)
    lines = [{"id": "line-1", "cameras": [{"camera_id": "m1", "role": "match", "match": {"reference_id": ids[0]}}]}]
    assert clean_up(lines) == 2                                             # ids[3] and the old draft
    left = sorted(p.name for p in (data_dir / "match-references").iterdir())
    assert left == ["drafts", ids[0], ids[1], ids[2], ids[4]]
    assert draft("m2") is not None and draft("m3") is None


def test_room(data_dir, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(match_references.shutil, "disk_usage", lambda path: usage(10, 10, 0))
    assert room_ok() is False
    monkeypatch.setattr(match_references.shutil, "disk_usage", lambda path: usage(10 ** 12, 0, 10 ** 12))
    assert room_ok() is True
